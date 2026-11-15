"""Offline-only tests: temporary SQLite files and fake HTTP/APNs boundaries."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from companion.report_notifications import (
    APNsTransport, BODY, Dispatcher, LEASE_SECONDS, MAX_ATTEMPTS,
    ReadyReport, SQLiteQueue, apns_payload,
)

NOW = datetime(2026, 11, 14, 23, tzinfo=timezone.utc)


class Authority:
    allowed = True
    terminal = True
    persisted = True

    def eligible(self, report):
        return self.allowed and self.terminal and self.persisted


class Transport:
    def __init__(self, outcome='accepted', action=None):
        self.calls = []
        self.outcome = outcome
        self.action = action

    async def send(self, job):
        self.calls.append(job)
        if self.action:
            self.action()
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def setup(tmp_path):
    queue = SQLiteQueue(str(tmp_path / 'notifications.sqlite'))
    queue.set_scope('account', 'epoch', 1, True)
    report = ReadyReport('account', 'epoch', 'report', 'registration', 1, NOW)
    yield queue, report, Authority()
    queue.close()


def status(queue, identifier):
    return queue.db.execute('SELECT status FROM report_notifications WHERE id=?', (identifier,)).fetchone()[0]


def run(queue, authority, transport, now=NOW):
    return asyncio.run(Dispatcher(queue, authority, transport, lambda: now).tick())


@pytest.mark.parametrize('field', ['allowed', 'terminal', 'persisted'])
def test_requires_authoritative_terminal_persistence_and_consent(setup, field):
    q, report, authority = setup
    setattr(authority, field, False)
    assert q.enqueue(report, authority) is None
    assert q.claim(NOW) is None


def test_due_time_utc_and_naive_rejected(setup):
    q, report, authority = setup
    with pytest.raises(ValueError):
        q.enqueue(replace(report, due_at=NOW.replace(tzinfo=None)), authority)
    q.enqueue(replace(report, due_at=NOW.astimezone(timezone(timedelta(hours=5)))), authority)
    assert q.claim(NOW - timedelta(microseconds=1)) is None
    assert q.claim(NOW).due == NOW.timestamp()


def test_durable_dedup_and_first_schedule_immutable(setup, tmp_path):
    q, report, authority = setup
    identifier = q.enqueue(report, authority)
    assert q.enqueue(replace(report, due_at=NOW + timedelta(days=1)), authority) == identifier
    other = SQLiteQueue(str(tmp_path / 'notifications.sqlite'))
    try:
        assert other.enqueue(report, authority) == identifier
        job = other.claim(NOW)
        assert q.claim(NOW) is None
        assert other.finish(job, 'accepted', NOW)
        assert q.enqueue(report, authority) == identifier
        assert q.claim(NOW + timedelta(days=2)) is None
    finally:
        other.close()


@pytest.mark.parametrize('change', ['logout', 'revoke', 'reset'])
def test_cancellation_survives_readmission_and_stale_updates(setup, change):
    q, report, authority = setup
    identifier = q.enqueue(report, authority)
    job = q.claim(NOW)
    epoch = 'new-epoch' if change == 'reset' else 'epoch'
    q.set_scope('account', epoch, 2, False)
    assert status(q, identifier) == 'cancelled'
    assert not q.finish(job, 'accepted', NOW)
    with pytest.raises(ValueError):
        q.set_scope('account', 'epoch', 1, True)
    q.set_scope('account', epoch, 3, True)
    assert q.claim(NOW) is None
    assert q.enqueue(report, authority) is None
    if change != 'reset':
        assert q.enqueue(replace(report, revision=3), authority) == identifier
        assert q.claim(NOW) is None


def test_cross_account_and_epoch_isolation(setup):
    q, report, authority = setup
    first = q.enqueue(report, authority)
    q.set_scope('other', 'epoch', 1, True)
    second = q.enqueue(replace(report, account='other'), authority)
    assert first != second
    q.set_scope('account', 'new-epoch', 2, True)
    third = q.enqueue(replace(report, epoch='new-epoch', revision=2), authority)
    assert third != first
    assert q.claim(NOW).id in {second, third}


@pytest.mark.parametrize('field', ['allowed', 'terminal', 'persisted'])
def test_dispatch_rechecks_consent_and_report(setup, field):
    q, report, authority = setup
    identifier = q.enqueue(report, authority)
    setattr(authority, field, False)
    transport = Transport()
    assert run(q, authority, transport)
    assert not transport.calls
    assert status(q, identifier) == 'cancelled'


def test_retry_backoff_then_acceptance_not_delivery(setup):
    q, report, authority = setup
    identifier = q.enqueue(report, authority)
    transport = Transport('retry')
    assert run(q, authority, transport)
    assert not run(q, authority, transport, NOW + timedelta(seconds=29))
    assert run(q, authority, transport, NOW + timedelta(seconds=30))
    assert not run(q, authority, transport, NOW + timedelta(seconds=89))
    transport.outcome = 'accepted'
    assert run(q, authority, transport, NOW + timedelta(seconds=90))
    assert status(q, identifier) == 'accepted'
    assert not run(q, authority, transport, NOW + timedelta(days=1))


def test_timeout_bounded_retries(setup):
    q, report, authority = setup
    identifier = q.enqueue(report, authority)
    transport = Transport(TimeoutError('must not persist private exception details'))
    for attempt in range(MAX_ATTEMPTS):
        assert run(q, authority, transport, NOW + timedelta(hours=attempt))
    assert status(q, identifier) == 'failed'
    assert not run(q, authority, transport, NOW + timedelta(days=1))


def test_crash_recovery_and_stale_lease_fencing(setup):
    q, report, authority = setup
    q.enqueue(report, authority)
    first = q.claim(NOW)
    assert q.claim(NOW + timedelta(seconds=LEASE_SECONDS - 1)) is None
    later = NOW + timedelta(seconds=LEASE_SECONDS)
    second = q.claim(later)
    assert first.id == second.id and first.lease != second.lease
    assert not q.finish(first, 'accepted', later)
    assert q.finish(second, 'accepted', later)


def test_crashes_eventually_exhaust_attempt_budget(setup):
    q, report, authority = setup
    identifier = q.enqueue(report, authority)
    for attempt in range(MAX_ATTEMPTS):
        assert q.claim(NOW + timedelta(seconds=attempt * LEASE_SECONDS))
    assert q.claim(NOW + timedelta(seconds=MAX_ATTEMPTS * LEASE_SECONDS)) is None
    assert status(q, identifier) == 'failed'


def test_revoke_between_authority_check_and_transport(setup):
    q, report, authority = setup
    identifier = q.enqueue(report, authority)

    class RevokingAuthority:
        def eligible(self, _):
            q.set_scope('account', 'epoch', 2, False)
            return True

    transport = Transport()
    run(q, RevokingAuthority(), transport)
    assert not transport.calls
    assert status(q, identifier) == 'cancelled'


def test_revoke_during_transport_cannot_be_overwritten(setup):
    q, report, authority = setup
    identifier = q.enqueue(report, authority)
    transport = Transport(action=lambda: q.set_scope('account', 'epoch', 2, False))
    run(q, authority, transport)
    assert status(q, identifier) == 'cancelled'
    assert len(transport.calls) == 1  # in-flight push cannot be recalled


def test_privacy_payload_and_dedup_per_registration(setup):
    q, report, authority = setup
    first = q.enqueue(report, authority)
    assert first != q.enqueue(replace(report, registration='second-device'), authority)
    job = q.claim(NOW)
    payload = apns_payload(job)
    assert payload == {'aps': {'alert': {'title': 'Your companion', 'body': BODY}}, 'report_notification_id': job.id}
    text = json.dumps(payload)
    assert 'account' not in text and 'epoch' not in text and 'registration' not in text


class Response:
    def __init__(self, code, reason):
        self.status_code, self.reason = code, reason

    def json(self):
        return {'reason': self.reason}


class HTTP:
    def __init__(self, code, reason):
        self.response = Response(code, reason)
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.mark.parametrize('code,reason,expected,invalid', [
    (200, None, 'accepted', False), (429, 'TooManyRequests', 'retry', False),
    (500, 'InternalServerError', 'retry', False), (503, 'Shutdown', 'retry', False),
    (410, 'Unregistered', 'cancelled', True), (400, 'BadDeviceToken', 'cancelled', True),
    (403, 'InvalidProviderToken', 'failed', False), (400, 'BadTopic', 'failed', False),
])
def test_apns_request_and_response_contract(setup, code, reason, expected, invalid):
    q, report, authority = setup
    q.enqueue(report, authority)
    job = q.claim(NOW)
    http, disabled = HTTP(code, reason), []
    transport = APNsTransport(http, 'test.bundle', True, lambda _: 'ab12', lambda: 'test-only', disabled.append)
    assert asyncio.run(transport.send(job)) == expected
    assert bool(disabled) == invalid
    url, request = http.calls[0]
    assert url == 'https://api.sandbox.push.apple.com/3/device/ab12'
    assert request['headers']['apns-expiration'] == '0'
    assert request['headers']['apns-push-type'] == 'alert'
    assert request['headers']['apns-priority'] == '5'
    assert request['headers']['apns-id'] == job.id
    assert len(request['headers']['apns-collapse-id'].encode()) <= 64
    assert request['json'] == apns_payload(job)


def test_revoked_registration_never_calls_http(setup):
    q, report, authority = setup
    q.enqueue(report, authority)
    job = q.claim(NOW)
    http = HTTP(200, None)
    transport = APNsTransport(http, 'test.bundle', False, lambda _: None, lambda: 'unused', lambda _: None)
    assert asyncio.run(transport.send(job)) == 'cancelled'
    assert not http.calls
