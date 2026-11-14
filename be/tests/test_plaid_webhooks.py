"""Offline scratch EC keys only: no Plaid credentials, live calls, or shared state."""
import asyncio
import base64
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
from datetime import datetime, timezone
import hashlib
import json
import socket

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
import pytest

from companion import plaid_webhooks as module
from companion.plaid_webhooks import PlaidWebhookVerifier, WebhookError

NOW = 1_794_672_000
BODY = {"item_id": "item_synthetic-1", "environment": "sandbox",
        "webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE"}


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


class Clock:
    def __init__(self):
        self.wall = NOW
        self.tick = 1000.0

    def advance(self, seconds):
        self.wall += seconds
        self.tick += seconds


class Provider:
    def __init__(self, clock):
        self.clock = clock
        self.keys = {}
        self.calls = []
        self.responses = {}
        self.error = None
        self.entered = asyncio.Event()
        self.release = None
        self.closed = False
        self.cancelled = False
        self.add("key-1")

    def add(self, kid):
        private = ec.generate_private_key(ec.SECP256R1())
        self.keys[kid] = private
        numbers = private.public_key().public_numbers()
        self.responses[kid] = {"key": {
            "alg": "ES256", "kid": kid, "crv": "P-256", "kty": "EC", "use": "sig",
            "x": b64(numbers.x.to_bytes(32, "big")),
            "y": b64(numbers.y.to_bytes(32, "big")),
            "created_at": NOW - 1000, "expired_at": None}, "request_id": "synthetic"}

    async def __call__(self, kid):
        self.calls.append(kid)
        self.entered.set()
        try:
            if self.release is not None:
                await self.release.wait()
            if self.error is not None:
                raise self.error
            if kid not in self.responses:
                raise WebhookError(401)
            return deepcopy(self.responses[kid])
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.closed = True

    def token(self, body, *, kid="key-1", header=None, claims=None,
              header_raw=None, claims_raw=None, private=None, der=False):
        header = {"alg": "ES256", "kid": kid, "typ": "JWT"} if header is None else header
        claims = {"iat": int(self.clock.wall), "request_body_sha256": hashlib.sha256(body).hexdigest()} if claims is None else claims
        head = b64(encoded(header) if header_raw is None else header_raw)
        payload = b64(encoded(claims) if claims_raw is None else claims_raw)
        message = (head + "." + payload).encode()
        signature = (private or self.keys.get(kid, self.keys["key-1"])).sign(message, ec.ECDSA(hashes.SHA256()))
        if not der:
            r, s = decode_dss_signature(signature)
            signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return message + b"." + b64(signature).encode()


def headers(token):
    return [(b"Plaid-Verification", token)]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("webhook verifier tests must remain offline")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
async def setup():
    clock = Clock()
    provider = Provider(clock)
    verifier = PlaidWebhookVerifier(provider, clock=lambda: clock.wall, monotonic=lambda: clock.tick)
    yield verifier, provider, clock
    await verifier.aclose()
    assert not verifier._inflight
    assert not provider.closed


async def reject(verifier, body, token, status=401):
    with pytest.raises(WebhookError) as result:
        await verifier.verify(body, headers(token))
    assert result.value.status == status
    assert result.value.retryable is (status == 503)
    assert str(result.value) in {"Webhook verification failed.", "Webhook verification unavailable."}
    assert result.value.__suppress_context__ or result.value.__context__ is None
    return result.value


async def test_frozen_event_and_exact_raw_whitespace(setup):
    verifier, provider, _ = setup
    body = json.dumps(BODY, indent=2).encode()
    event = await verifier.verify(body, [(b"pLaId-vErIfIcAtIoN", provider.token(body))])
    assert asdict(event) == {**BODY, "body_digest": hashlib.sha256(body).hexdigest(),
                             "received_at": datetime.fromtimestamp(NOW, timezone.utc)}
    with pytest.raises(FrozenInstanceError):
        event.item_id = "other"
    await reject(verifier, encoded(BODY), provider.token(body))
    await reject(verifier, body + b"\n", provider.token(body))
    changed = encoded({**BODY, "item_id": "other"})
    await reject(verifier, changed, provider.token(body))
    assert provider.calls == ["key-1"]


async def test_untrusted_owner_metadata_dropped_and_other_event_is_generic(setup):
    verifier, provider, _ = setup
    body = encoded({**BODY, "webhook_type": "ITEM", "webhook_code": "ERROR",
                    "user_id": "victim", "owner_id": "victim", "client_user_id": "victim",
                    "metadata": {"user_id": "victim"}, "records": [{"private": "raw"}]})
    event = await verifier.verify(body, headers(provider.token(body)))
    assert event.webhook_type == "ITEM"
    assert event.webhook_code == "ERROR"
    assert set(asdict(event)) == {"item_id", "environment", "webhook_type", "webhook_code", "body_digest", "received_at"}
    assert "victim" not in repr(event) and "records" not in repr(event)


@pytest.mark.parametrize("delta,valid", [(-301, False), (-300, True), (0, True), (5, True), (6, False)])
async def test_iat_boundaries(setup, delta, valid):
    verifier, provider, _ = setup
    body = encoded(BODY)
    token = provider.token(body, claims={"iat": NOW + delta, "request_body_sha256": hashlib.sha256(body).hexdigest()})
    if valid:
        assert (await verifier.verify(body, headers(token))).item_id == BODY["item_id"]
    else:
        await reject(verifier, body, token)
        assert not provider.calls


@pytest.mark.parametrize("value", [True, False, None, str(NOW), float(NOW), [], {}, -1, 10**40])
async def test_strict_iat_type(setup, value):
    verifier, provider, _ = setup
    body = encoded(BODY)
    token = provider.token(body, claims={"iat": value, "request_body_sha256": "0" * 64})
    await reject(verifier, body, token)
    assert not provider.calls


@pytest.mark.parametrize("change", [
    {"alg": "HS256"}, {"alg": "none"}, {"alg": "ES384"}, {"alg": []},
    {"crit": []}, {"jku": "https://attacker.invalid/key"}, {"jwk": {}},
    {"x5u": "https://attacker.invalid/key"}, {"b64": False}, {"typ": "other"},
    {"kid": "https://attacker.invalid"}, {"kid": "../key"}, {"kid": "a" * 257},
    {"kid": True}, {"kid": None},
])
async def test_header_algorithm_confusion_and_untrusted_key_sources(setup, change):
    verifier, provider, _ = setup
    body = encoded(BODY)
    token = provider.token(body, header={"alg": "ES256", "kid": "key-1", **change})
    await reject(verifier, body, token)
    assert not provider.calls


@pytest.mark.parametrize("raw,field", [
    (b'{"alg":"HS256","alg":"ES256","kid":"key-1"}', "header_raw"),
    (b'{"alg":"ES256","kid":"key-1","kid":"key-1"}', "header_raw"),
    (b'{"iat":1,"iat":1794672000,"request_body_sha256":"' + b"0" * 64 + b'"}', "claims_raw"),
    (b'{"iat":NaN,"request_body_sha256":"' + b"0" * 64 + b'"}', "claims_raw"),
    (b'{"iat":Infinity}', "claims_raw"),
    (b'[]', "header_raw"), (b'[]', "claims_raw"), (b'\xff', "header_raw"),
    (b'{"iat":' + b'1' * 65 + b'}', "claims_raw"),
    (b'{"nested":' + b'[' * 100 + b'0' + b']' * 100 + b'}', "claims_raw"),
])
async def test_duplicate_and_malformed_jwt_json(setup, raw, field):
    verifier, provider, _ = setup
    body = encoded(BODY)
    await reject(verifier, body, provider.token(body, **{field: raw}))
    assert not provider.calls


@pytest.mark.parametrize("value", [None, 42, [], "0" * 63, "A" * 64, "g" * 64])
async def test_hash_claim_shape(setup, value):
    verifier, provider, _ = setup
    body = encoded(BODY)
    await reject(verifier, body, provider.token(body, claims={"iat": NOW, "request_body_sha256": value}))
    assert not provider.calls


async def test_claim_allowlist_and_wrong_hash(setup):
    verifier, provider, _ = setup
    body = encoded(BODY)
    await reject(verifier, body, provider.token(body, claims={"iat": NOW, "request_body_sha256": "0" * 64, "exp": NOW + 600}))
    assert not provider.calls
    await reject(verifier, body, provider.token(body, claims={"iat": NOW, "request_body_sha256": "0" * 64}))


async def test_duplicate_missing_and_oversized_headers(setup):
    verifier, provider, _ = setup
    body = encoded(BODY)
    token = provider.token(body)
    cases = [[], headers(token) + [(b"plaid-verification", token)],
             headers(token + b"," + token), headers(b"x" * (module.MAX_TOKEN_BYTES + 1)),
             headers(token) + [(b"x", b"y")] * module.MAX_HEADERS,
             headers(token) + [(b"x", b"y" * module.MAX_HEADER_BYTES)],
             [("Plaid-Verification", token)], [(b"Plaid-Verification", "token")],
             [[b"Plaid-Verification", token]], [(b"Plaid-Verification",)], {}, None]
    for raw_headers in cases:
        with pytest.raises(WebhookError) as result:
            await verifier.verify(body, raw_headers)
        assert result.value.status == 401
    assert not provider.calls


@pytest.mark.parametrize("token", [b"", b"a", b"a.b", b"a.b.c.d", b"a.b.c", b"=.e.e", b"\xff", b"a\nb.c.d"])
async def test_invalid_jwt_encodings(setup, token):
    verifier, provider, _ = setup
    await reject(verifier, encoded(BODY), token)
    assert not provider.calls


async def test_signature_shapes_wrong_key_and_no_refresh_for_bad_signature(setup):
    verifier, provider, _ = setup
    body = encoded(BODY)
    token = provider.token(body)
    head = token.rsplit(b".", 1)[0]
    for signature in [b"", b64(b"\x00" * 63).encode(), b64(b"\x00" * 65).encode(), b64(b"\x00" * 64).encode()]:
        await reject(verifier, body, head + b"." + signature)
    await reject(verifier, body, provider.token(body, der=True))
    other = ec.generate_private_key(ec.SECP256R1())
    for _ in range(20):
        await reject(verifier, body, provider.token(body, private=other))
    await reject(verifier, body, token + b"=")
    assert provider.calls == ["key-1"]


async def test_no_body_json_parse_before_signature_and_hash(setup, monkeypatch):
    verifier, provider, _ = setup
    body = b'not-json-private-body'
    original = module._json
    seen = []

    def spy(raw, limit):
        seen.append(raw)
        return original(raw, limit)

    monkeypatch.setattr(module, "_json", spy)
    bad_key = ec.generate_private_key(ec.SECP256R1())
    await reject(verifier, body, provider.token(body, private=bad_key))
    assert body not in seen
    await reject(verifier, body, provider.token(body, claims={"iat": NOW, "request_body_sha256": "0" * 64}))
    assert body not in seen
    await reject(verifier, body, provider.token(body))
    assert body in seen


@pytest.mark.parametrize("change", [
    {"item_id": None}, {"item_id": 123}, {"item_id": True}, {"item_id": ""},
    {"item_id": "a" * 257}, {"item_id": "a b"}, {"item_id": "a\n"},
    {"environment": "production"}, {"environment": None}, {"environment": ["sandbox"]},
    {"webhook_type": "transactions"}, {"webhook_code": None},
])
async def test_signed_body_required_field_types(setup, change):
    verifier, provider, _ = setup
    body = encoded({**BODY, **change})
    await reject(verifier, body, provider.token(body))


@pytest.mark.parametrize("raw", [
    b'{"item_id":"a","item_id":"b"}', b'{"x":NaN}', b'{"x":1e999}',
    b'{"x":{"user_id":"a","user_id":"b"}}', b'[]', b'null', b'\xff',
    b'{"x":' + b'[' * 1000 + b'0' + b']' * 1000 + b'}',
    b'{"x":[' + b'0,' * 4096 + b'0]}',
])
async def test_signed_body_json_budgets(setup, raw):
    verifier, provider, _ = setup
    await reject(verifier, raw, provider.token(raw))


async def test_body_byte_budgets_and_no_text_coercion(setup):
    verifier, provider, _ = setup
    token = provider.token(encoded(BODY))
    for body in [b"", b"x" * (module.MAX_BODY_BYTES + 1), "body", bytearray(b"body"), None]:
        await reject(verifier, body, token)
    assert not provider.calls


@pytest.mark.parametrize("change,status", [
    ({"expired_at": NOW}, 401), ({"expired_at": NOW - 1}, 401),
    ({"created_at": NOW + 6}, 401), ({"created_at": True}, 503),
    ({"expired_at": False}, 503), ({"expired_at": "today"}, 503),
    ({"alg": "HS256"}, 503), ({"kty": "RSA"}, 503), ({"crv": "P-384"}, 503),
    ({"kid": "key-other"}, 503), ({"use": "enc"}, 503), ({"key_ops": ["sign"]}, 503),
    ({"d": "private"}, 503), ({"x": "x" * 44}, 503), ({"x": []}, 503),
    ({"x": b64(b"\x00" * 32), "y": b64(b"\x00" * 32)}, 503),
])
async def test_provider_key_validation(setup, change, status):
    verifier, provider, _ = setup
    provider.responses["key-1"]["key"].update(change)
    body = encoded(BODY)
    await reject(verifier, body, provider.token(body), status)
    await reject(verifier, body, provider.token(body), status)
    assert provider.calls == ["key-1"]


@pytest.mark.parametrize("response", [None, [], {}, {"key": None}, {"key": {}},
                                       {"oversized": "x" * 9000}, {"nested": list(range(129))}])
async def test_key_response_budgets(setup, response):
    verifier, provider, _ = setup
    provider.responses["key-1"] = response
    body = encoded(BODY)
    await reject(verifier, body, provider.token(body), 503)


async def test_key_expiration_checked_on_cache_hits(setup):
    verifier, provider, clock = setup
    provider.responses["key-1"]["key"]["expired_at"] = NOW + 1
    body = encoded(BODY)
    await verifier.verify(body, headers(provider.token(body)))
    clock.advance(1)
    await reject(verifier, body, provider.token(body))
    assert provider.calls == ["key-1"]


async def test_ttl_refresh_rotation_and_revocation(setup):
    verifier, provider, clock = setup
    body = encoded(BODY)
    await verifier.verify(body, headers(provider.token(body)))
    provider.add("key-2")
    await verifier.verify(body, headers(provider.token(body, kid="key-2")))
    assert provider.calls == ["key-1", "key-2"]
    clock.advance(module.KEY_TTL_SECONDS)
    provider.add("key-1")  # refreshed key bytes must replace the old public key
    await verifier.verify(body, headers(provider.token(body)))
    assert provider.calls == ["key-1", "key-2", "key-1"]
    provider.responses["key-1"]["key"]["expired_at"] = int(clock.wall)
    clock.advance(module.KEY_TTL_SECONDS)
    await reject(verifier, body, provider.token(body))


async def test_cache_is_bounded_and_lru(setup):
    verifier, provider, clock = setup
    body = encoded(BODY)
    # Advance monotonic rate windows only, preserving TTL to exercise eviction.
    for i in range(module.MAX_CACHE_KEYS + 1):
        kid = f"key-{i}"
        provider.add(kid)
        await verifier.verify(body, headers(provider.token(body, kid=kid)))
        if i % 7 == 6:
            clock.tick += 60
    assert len(verifier._cache) == module.MAX_CACHE_KEYS
    assert "key-0" not in verifier._cache
    assert "key-16" in verifier._cache


async def test_unknown_key_flood_negative_cache_and_global_budget(setup):
    verifier, provider, clock = setup
    body = encoded(BODY)
    for i in range(50):
        await reject(verifier, body, provider.token(body, kid=f"unknown-{i}"),
                     401 if i < module.MAX_LOOKUPS_PER_MINUTE else 503)
    assert len(provider.calls) == module.MAX_LOOKUPS_PER_MINUTE
    await reject(verifier, body, provider.token(body, kid="unknown-0"))
    assert len(provider.calls) == module.MAX_LOOKUPS_PER_MINUTE
    clock.advance(60)
    await verifier.verify(body, headers(provider.token(body)))


async def test_failure_cache_is_bounded(setup):
    verifier, provider, clock = setup
    body = encoded(BODY)
    for i in range(40):
        await reject(verifier, body, provider.token(body, kid=f"unknown-{i}"))
        clock.advance(60)
    assert len(verifier._failures) <= module.MAX_CACHE_KEYS
    assert len(verifier._lookups) <= module.MAX_LOOKUPS_PER_MINUTE


async def test_outage_global_cooldown_then_recovery_and_cached_keys_work(setup, caplog):
    verifier, provider, clock = setup
    body = encoded(BODY)
    await verifier.verify(body, headers(provider.token(body)))
    provider.error = RuntimeError("PRIVATE_TOKEN PRIVATE_RESPONSE")
    await reject(verifier, body, provider.token(body, kid="new-key"), 503)
    for i in range(25):
        await reject(verifier, body, provider.token(body, kid=f"other-{i}"), 503)
    assert provider.calls == ["key-1", "new-key"]
    await verifier.verify(body, headers(provider.token(body)))
    clock.advance(module.FAILURE_COOLDOWN_SECONDS)
    provider.error = None
    provider.add("new-key")
    await verifier.verify(body, headers(provider.token(body, kid="new-key")))
    assert not caplog.records


async def test_timeout_maps_to_503_and_does_not_leak_provider_message(setup, monkeypatch):
    verifier, provider, _ = setup
    monkeypatch.setattr(module, "PROVIDER_TIMEOUT_SECONDS", 0.01)
    provider.release = asyncio.Event()
    body = encoded(BODY)
    await reject(verifier, body, provider.token(body), 503)
    assert provider.cancelled
    assert not verifier._inflight


async def test_concurrent_notifications_singleflight(setup):
    verifier, provider, _ = setup
    provider.release = asyncio.Event()
    bodies = [encoded({**BODY, "initial_update_complete": i % 2 == 0, "count": i}) for i in range(40)]
    tasks = [asyncio.create_task(verifier.verify(body, headers(provider.token(body)))) for body in bodies]
    await provider.entered.wait()
    assert provider.calls == ["key-1"]
    provider.release.set()
    results = await asyncio.gather(*tasks)
    assert len(results) == 40
    assert len({event.body_digest for event in results}) == 40
    assert provider.calls == ["key-1"]


async def test_cancelled_waiter_does_not_cancel_shared_lookup(setup):
    verifier, provider, _ = setup
    provider.release = asyncio.Event()
    body = encoded(BODY)
    token = provider.token(body)
    first = asyncio.create_task(verifier.verify(body, headers(token)))
    second = asyncio.create_task(verifier.verify(body, headers(token)))
    await provider.entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not provider.cancelled
    provider.release.set()
    assert (await second).item_id == BODY["item_id"]
    assert provider.calls == ["key-1"]


async def test_close_drains_owned_tasks_but_not_provider(setup):
    verifier, provider, _ = setup
    provider.release = asyncio.Event()
    body = encoded(BODY)
    task = asyncio.create_task(verifier.verify(body, headers(provider.token(body))))
    await provider.entered.wait()
    await verifier.aclose()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled and not provider.closed
    assert not verifier._inflight
    await reject(verifier, body, provider.token(body), 503)
    await verifier.aclose()


async def test_age_and_expiry_rechecked_after_provider_wait(setup):
    verifier, provider, clock = setup
    provider.release = asyncio.Event()
    body = encoded(BODY)
    task = asyncio.create_task(verifier.verify(body, headers(provider.token(body))))
    await provider.entered.wait()
    clock.advance(301)
    provider.release.set()
    with pytest.raises(WebhookError) as error:
        await task
    assert error.value.status == 401


async def test_inflight_budget_under_distinct_key_flood(setup):
    verifier, provider, _ = setup
    provider.release = asyncio.Event()
    body = encoded(BODY)
    tasks = []
    for i in range(module.MAX_INFLIGHT):
        kid = f"key-{i}"
        provider.add(kid)
        tasks.append(asyncio.create_task(verifier.verify(body, headers(provider.token(body, kid=kid)))))
    await provider.entered.wait()
    await reject(verifier, body, provider.token(body, kid="overflow"), 503)
    provider.release.set()
    assert len(await asyncio.gather(*tasks)) == module.MAX_INFLIGHT
    assert len(provider.calls) == module.MAX_INFLIGHT


async def test_hash_comparison_uses_constant_time(setup, monkeypatch):
    verifier, provider, _ = setup
    body = encoded(BODY)
    original = module.hmac.compare_digest
    comparisons = []

    def compare(left, right):
        comparisons.append((left, right))
        return original(left, right)

    monkeypatch.setattr(module.hmac, "compare_digest", compare)
    await verifier.verify(body, headers(provider.token(body)))
    digest = hashlib.sha256(body).hexdigest()
    assert comparisons == [(digest, digest)]


async def test_cancellation_of_all_waiters_consumes_later_provider_failure(setup):
    verifier, provider, _ = setup
    provider.release = asyncio.Event()
    provider.error = RuntimeError("private-provider-error")
    body = encoded(BODY)
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        waiter = asyncio.create_task(verifier.verify(body, headers(provider.token(body))))
        await provider.entered.wait()
        fetch = verifier._inflight["key-1"]
        finished = asyncio.Event()
        fetch.add_done_callback(lambda _done: finished.set())
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        provider.release.set()
        await finished.wait()
        assert not verifier._inflight
        assert not fetch.cancelled()
        assert not fetch._log_traceback  # done callback retrieved the safe exception
        assert not unhandled
    finally:
        loop.set_exception_handler(previous)


async def test_provider_cancellation_is_not_translated_to_auth_failure(setup):
    verifier, provider, _ = setup
    provider.error = asyncio.CancelledError()
    body = encoded(BODY)
    with pytest.raises(asyncio.CancelledError):
        await verifier.verify(body, headers(provider.token(body)))
    assert not verifier._inflight
    provider.error = None
    await verifier.verify(body, headers(provider.token(body)))


async def test_received_at_is_ingress_time_not_provider_completion(setup):
    verifier, provider, clock = setup
    provider.release = asyncio.Event()
    body = encoded(BODY)
    waiter = asyncio.create_task(verifier.verify(body, headers(provider.token(body))))
    await provider.entered.wait()
    clock.advance(10)
    provider.release.set()
    assert (await waiter).received_at == datetime.fromtimestamp(NOW, timezone.utc)


async def test_key_created_after_iat_rejected(setup):
    verifier, provider, _ = setup
    provider.responses["key-1"]["key"]["created_at"] = NOW
    body = encoded(BODY)
    await reject(verifier, body, provider.token(body, claims={
        "iat": NOW - 20, "request_body_sha256": hashlib.sha256(body).hexdigest()}))


async def test_noncanonical_base64url_rejected(setup):
    verifier, provider, _ = setup
    body = encoded(BODY)
    token = provider.token(body)
    # A 64-byte ES256 signature ends in a base64 sextet with four zero pad bits.
    alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    index = alphabet.index(token[-1])
    altered = token[:-1] + bytes([alphabet[index + 1]])
    assert base64.urlsafe_b64decode(token.rsplit(b".", 1)[1] + b"==") == base64.urlsafe_b64decode(altered.rsplit(b".", 1)[1] + b"==")
    await reject(verifier, body, altered)
    assert not provider.calls


@pytest.mark.parametrize("field", ["created_at", "expired_at", "kid", "alg", "x", "y"])
async def test_missing_provider_key_fields_fail_closed(setup, field):
    verifier, provider, _ = setup
    del provider.responses["key-1"]["key"][field]
    body = encoded(BODY)
    await reject(verifier, body, provider.token(body), 503)


@pytest.mark.parametrize("field", ["item_id", "environment", "webhook_type", "webhook_code"])
async def test_missing_signed_body_fields(setup, field):
    verifier, provider, _ = setup
    payload = dict(BODY)
    del payload[field]
    body = encoded(payload)
    await reject(verifier, body, provider.token(body))
