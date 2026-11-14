"""Offline security tests: all RSA material is generated scratch material."""
import asyncio
import base64
from dataclasses import FrozenInstanceError
import json
import time
from urllib.parse import urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from companion.auth import (AuthConfig, AuthConfigurationError, AuthError, AuthVerifier,
                            JWKSCache, Principal, verifier_from_env)

ISSUER = "https://clerk.example.com"
NOW = 1800000000


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwk(key, kid="scratch"):
    numbers = key.public_key().public_numbers()
    return {"kty": "RSA", "kid": kid, "alg": "RS256", "use": "sig",
            "n": b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big"))}


def claims(**updates):
    return dict(iss=ISSUER, sub="user_scratch", sid="session_scratch", v=2,
                iat=NOW - 20, nbf=NOW - 20, exp=NOW + 60) | updates


def token(key, payload=None, header=None, raw_header=None, raw_payload=None):
    h = raw_header if raw_header is not None else json.dumps(header or {"alg": "RS256", "kid": "scratch"}).encode()
    c = raw_payload if raw_payload is not None else json.dumps(claims() if payload is None else payload).encode()
    message = (b64(h) + "." + b64(c)).encode()
    return message.decode() + "." + b64(key.sign(message, padding.PKCS1v15(), hashes.SHA256()))


class Stream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        for offset in range(0, len(self.data), 4096):
            yield self.data[offset:offset + 4096]


class Harness:
    def __init__(self, keys, config=None, ttl=300):
        self.keys = keys
        self.clock = 1000.0
        self.calls = 0
        self.document = {"keys": [jwk(keys[0])]}
        self.failure = False
        self.status = 200
        self.extra_headers = {}
        self.body = None
        self.addresses = ["93.184.216.34"]
        self.delay = 0
        self.config = config or AuthConfig(ISSUER)
        self.cache = JWKSCache(self.config, transport=httpx.MockTransport(self.fetch),
                               resolver=self.resolve, monotonic=lambda: self.clock, ttl=ttl)
        self.verifier = AuthVerifier(self.config, cache=self.cache, clock=lambda: NOW)

    async def resolve(self, host, port):
        assert host == urlsplit(self.config.issuer).hostname and port == 443
        return self.addresses

    async def fetch(self, request):
        self.calls += 1
        assert str(request.url) == "https://93.184.216.34/.well-known/jwks.json"
        assert request.headers["host"] == urlsplit(self.config.issuer).netloc
        assert request.extensions["sni_hostname"] == urlsplit(self.config.issuer).hostname
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            raise httpx.ConnectError("scratch network failure")
        return httpx.Response(self.status, headers=self.extra_headers,
                              stream=Stream(self.body if self.body is not None else json.dumps(self.document).encode()))

    async def verify(self, value=None, **updates):
        return await self.verifier.verify_token(value or token(self.keys[0], claims(**updates)))


@pytest.fixture(scope="module")
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(3)]


@pytest.fixture
async def harness(keys):
    h = Harness(keys)
    yield h
    await h.verifier.aclose()


async def failure(h, value=None, code="unauthorized", **updates):
    with pytest.raises(AuthError) as error:
        await h.verify(value, **updates)
    assert error.value.code == code
    assert str(error.value) == code
    assert error.value.retryable == (code == "auth_unavailable")
    assert error.value.headers == ({"WWW-Authenticate": "Bearer"} if code == "unauthorized" else {})
    return error.value


async def test_verified_principal_and_request(harness):
    jwt = token(harness.keys[0])
    p = await harness.verifier.authenticate_request([(b"authorization", ("Bearer " + jwt).encode())])
    assert (p.issuer, p.subject, p.session_id) == (ISSUER, "user_scratch", "session_scratch")
    with pytest.raises(FrozenInstanceError):
        p.subject = "another"
    with pytest.raises(TypeError):
        Principal(ISSUER, "not_verified", "session")
    assert harness.calls == 1


@pytest.mark.parametrize("field", ["iss", "sub", "sid", "v", "exp", "nbf", "iat"])
async def test_required_claims(harness, field):
    c = claims()
    del c[field]
    await failure(harness, token(harness.keys[0], c))


@pytest.mark.parametrize("field,value", [
    ("iss", "https://wrong.example.com"), ("iss", ISSUER + "/"), ("iss", None),
    ("sub", ""), ("sub", None), ("sub", 12), ("sub", "x" * 257),
    ("sid", ""), ("sid", True), ("sid", "x" * 257), ("sid", "nul\x00"),
    ("v", True), ("v", "2"), ("v", 1), ("act", None), ("act", {}),
    ("exp", NOW - 5), ("nbf", NOW + 6), ("iat", NOW + 6),
    ("exp", NOW - 20), ("exp", True), ("exp", str(NOW)), ("exp", None),
    ("nbf", False), ("nbf", 1.2), ("iat", None), ("iat", float("nan")),
    ("sts", None), ("sts", "unknown"), ("sts", []),
    ("azp", None), ("azp", ""), ("azp", "https://evil.example"),
    ("aud", None), ("aud", "api"), ("aud", ["api"]),
])
async def test_bad_claims(harness, field, value):
    await failure(harness, **{field: value})


@pytest.mark.parametrize("field", ["exp", "nbf", "iat"])
@pytest.mark.parametrize("bad", [True, False, None, "1800000000", 1800000000.0, [], {}, -1, 253402300800])
async def test_all_timestamp_type_boundaries(harness, field, bad):
    await failure(harness, **{field: bad})


async def test_leeway_and_pending(harness):
    await harness.verify(exp=NOW - 4, iat=NOW - 30, nbf=NOW - 30)
    await harness.verify(nbf=NOW + 5, iat=NOW + 5)
    await harness.verify(sts="active")
    await failure(harness, code="session_pending", sts="pending")
    # Invalid claims do not become a verified pending session.
    await failure(harness, sts="pending", iss="wrong")
    await failure(harness, exp=NOW + 20, nbf=NOW + 20)


@pytest.mark.parametrize("aud", ["api", ["other", "api"]])
async def test_explicit_audience_and_native_azp(keys, aud):
    h = Harness(keys, AuthConfig(ISSUER, "api", ("https://app.example",)))
    try:
        await h.verify(aud=aud)
        await h.verify(aud=aud, azp="https://app.example")
        await failure(h, aud=aud, azp="https://app.example/")
        for bad in [None, [], ["api", None], ["other"], "other", True, ["api"] * 65]:
            await failure(h, aud=bad)
        await failure(h)  # Configured recipient is required.
    finally:
        await h.verifier.aclose()


@pytest.mark.parametrize("header", [
    {"alg": "none", "kid": "scratch"}, {"alg": "HS256", "kid": "scratch"},
    {"alg": "RS512", "kid": "scratch"}, {"alg": "RS256"},
    {"alg": "RS256", "kid": ""}, {"alg": "RS256", "kid": "x" * 129},
    {"alg": "RS256", "kid": True},
    *[{"alg": "RS256", "kid": "scratch", name: value}
      for name, value in [("jku", "https://evil.example"), ("jwk", {}), ("x5u", "https://evil.example"),
                          ("crit", []), ("crit", ["oiat"]), ("b64", False)]],
])
async def test_jose_headers_fail_before_network(harness, header):
    await failure(harness, token(harness.keys[0], header=header))
    assert harness.calls == 0


async def test_harmless_header_and_duplicate_json(harness):
    await harness.verify(token(harness.keys[0], header={"alg": "RS256", "kid": "scratch", "oiat": NOW}))
    for raw in [b'{"alg":"RS256","alg":"RS256","kid":"scratch"}', b'{}', b'[]', b'{"a":"' + b'a' * 2048 + b'"}']:
        await failure(harness, token(harness.keys[0], raw_header=raw))
    await failure(harness, token(harness.keys[0], raw_payload=b'{"sub":"a","sub":"b"}'))
    await failure(harness, token(harness.keys[0], raw_payload=b'[' * 3000 + b']' * 3000))


@pytest.mark.parametrize("value", [b"", b"Basic abc", b"Bearer", b"Bearer  abc", b"Bearer abc ",
                                    b"Bearer\tabc", b"Bearer abc\n", b"Bearer \xff", b"Bearer " + b"x" * 16384])
async def test_bad_authorization(harness, value):
    for headers in [[(b"authorization", value)], [], [(b"Authorization", value), (b"authorization", value)]]:
        with pytest.raises(AuthError) as error:
            await harness.verifier.authenticate_request(headers)
        assert error.value.status_code == 401
    assert harness.calls == 0


@pytest.mark.parametrize("value", ["a.b.c", "a.b", ".a.b", "a.b.c.d", "a=.b.c", "é.a.b", "x" * 16384])
async def test_malformed_jwt(harness, value):
    await failure(harness, value)
    assert harness.calls == 0


async def test_valid_credential_duplicate_and_combined_headers_rejected(harness):
    value = ("Bearer " + token(harness.keys[0])).encode()
    for headers in [[(b"authorization", value), (b"Authorization", value)],
                    [(b"authorization", value + b", " + value)]]:
        with pytest.raises(AuthError):
            await harness.verifier.authenticate_request(headers)
    assert harness.calls == 0
    p = await harness.verifier.authenticate_request([(b"AUTHORIZATION", b"bearer " + value[7:])])
    assert p.subject == "user_scratch"


async def test_wrong_signature_and_cooldown(harness):
    await failure(harness, token(harness.keys[1]))
    assert harness.calls == 1
    await harness.verify()
    harness.clock += 31
    await failure(harness, token(harness.keys[1]))
    assert harness.calls == 2  # One gated same-kid refresh.


async def test_unknown_kid_flood_singleflight_and_rotation(harness):
    harness.delay = 0.01
    def unknown(kid):
        return token(harness.keys[0], header={"alg": "RS256", "kid": kid})
    results = await asyncio.gather(*(harness.verify(unknown(str(i))) for i in range(40)), return_exceptions=True)
    assert all(isinstance(x, AuthError) and x.code == "unauthorized" for x in results)
    assert harness.calls == 1
    harness.document = {"keys": [jwk(harness.keys[1], "rotated")]}
    await failure(harness, token(harness.keys[1], header={"alg": "RS256", "kid": "rotated"}))
    harness.clock += 30
    await harness.verify(token(harness.keys[1], header={"alg": "RS256", "kid": "rotated"}))
    assert harness.calls == 2
    await failure(harness)  # Atomic replacement removed old key.


async def test_cancelled_caller_does_not_cancel_shared_refresh(harness):
    harness.delay = 0.05
    first = asyncio.create_task(harness.verify())
    while harness.calls == 0:
        await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await harness.verify()).subject == "user_scratch"
    assert harness.calls == 1


async def test_no_token_in_logs_or_errors(harness, caplog):
    caplog.set_level("DEBUG")
    jwt = token(harness.keys[0])
    await harness.verify(jwt)
    bad = token(harness.keys[1])
    error = await failure(harness, bad)
    assert jwt not in caplog.text and bad not in caplog.text
    assert jwt not in str(error) and bad not in str(error)
    await failure(harness, token(harness.keys[0], raw_payload=b'{"exp":1e999}'))


async def test_failed_same_kid_refresh_is_unavailable(harness):
    await harness.verify()
    harness.clock += 30
    harness.failure = True
    await failure(harness, token(harness.keys[1]), code="auth_unavailable")
    await failure(harness, token(harness.keys[1]), code="auth_unavailable")
    await harness.verify()  # Good signature with cached unexpired key still works.
    assert harness.calls == 2


async def test_failed_parse_never_extends_cached_ttl(harness):
    await harness.verify()
    harness.document = {"keys": []}
    harness.clock += 30
    unknown = token(harness.keys[0], header={"alg": "RS256", "kid": "new"})
    await failure(harness, unknown, code="auth_unavailable")
    await harness.verify()
    harness.clock = 1300
    await failure(harness, code="auth_unavailable")
    assert harness.calls == 3


async def test_initial_outage_and_startup_fail_closed(harness):
    harness.failure = True
    for _ in range(5):
        with pytest.raises(AuthError) as err:
            await harness.verifier.start()
        assert err.value.status_code == 503
    assert harness.calls == 1


async def test_same_kid_rotation(harness):
    await harness.verify()
    harness.document = {"keys": [jwk(harness.keys[1])]}
    await failure(harness, token(harness.keys[1]))  # Cooldown deliberately rejects rotation.
    harness.clock += 30
    await harness.verify(token(harness.keys[1]))
    assert harness.calls == 2


async def test_outage_preserves_unexpired_never_stale_and_bounds_failures(harness):
    await harness.verify()
    harness.failure = True
    harness.clock += 31
    unknown = token(harness.keys[0], header={"alg": "RS256", "kid": "unknown"})
    await failure(harness, unknown, code="auth_unavailable")
    await harness.verify()  # Still trusted unexpired key.
    results = await asyncio.gather(*(harness.verify(unknown) for _ in range(20)), return_exceptions=True)
    assert all(x.code == "auth_unavailable" for x in results)
    assert harness.calls == 2
    harness.clock = 1300
    await failure(harness, code="auth_unavailable")
    assert harness.calls == 3
    await failure(harness, code="auth_unavailable")
    assert harness.calls == 3
    harness.clock += 30
    harness.failure = False
    await harness.verify()
    assert harness.calls == 4


@pytest.mark.parametrize("bad", [
    {"keys": []}, {"keys": [{}]}, {"keys": "wrong"}, [],
    {"keys": [{"kid": "a", "kty": "oct"}]},
])
async def test_invalid_jwks(harness, bad):
    harness.document = bad
    await failure(harness, code="auth_unavailable")
    await failure(harness, code="auth_unavailable")
    assert harness.calls == 1


@pytest.mark.parametrize("change", ["duplicate", "count", "alg", "use", "ops", "small", "private", "exponent"])
async def test_unsafe_key_sets(harness, change):
    k = jwk(harness.keys[0])
    if change == "duplicate":
        harness.document = {"keys": [k, k]}
    elif change == "count":
        harness.document = {"keys": [dict(k, kid=str(i)) for i in range(33)]}
    else:
        k.update({"alg": {"alg": "HS256"}, "use": {"use": "enc"}, "ops": {"key_ops": ["sign"]},
                  "small": {"n": b64(b'123')}, "private": {"d": "abc"}, "exponent": {"e": b64(b'\x02')}}[change])
        harness.document = {"keys": [k]}
    await failure(harness, code="auth_unavailable")


@pytest.mark.parametrize("mode", ["bytes", "length", "redirect", "compressed", "duplicate_json"])
async def test_response_limits(harness, mode):
    if mode == "bytes":
        harness.body = b" " * (256 * 1024 + 1)
    elif mode == "length":
        harness.extra_headers = {"Content-Length": str(256 * 1024 + 1)}
    elif mode == "redirect":
        harness.status, harness.extra_headers = 302, {"Location": "http://169.254.169.254"}
    elif mode == "compressed":
        harness.extra_headers = {"Content-Encoding": "gzip"}
    else:
        harness.body = b'{"keys":[],"keys":[]}'
    await failure(harness, code="auth_unavailable")
    assert harness.calls == 1


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "10.0.0.1", "169.254.169.254", "0.0.0.0",
                                      "::ffff:127.0.0.1", "fc00::1", "224.0.0.1", "100.64.0.1"])
async def test_private_dns_never_connects(harness, address):
    harness.addresses = ["93.184.216.34", address]
    await failure(harness, code="auth_unavailable")
    assert harness.calls == 0


async def test_total_deadline_includes_dns(harness):
    async def stall(*args):
        await asyncio.sleep(10)
    harness.cache._resolver = stall
    before = time.monotonic()
    await failure(harness, code="auth_unavailable")
    assert 4.5 <= time.monotonic() - before < 7
    await failure(harness, code="auth_unavailable")
    assert harness.calls == 0


@pytest.mark.parametrize("issuer", ["", "http://clerk.example.com", "https://clerk.example.com/", "https://a:b@clerk.example.com",
    "https://clerk.example.com?q=x", "https://clerk.example.com#x", "https://localhost", "https://127.0.0.1",
    "https://169.254.169.254", "https://[::1]", "https://clerk.example.com:8443", "https://clerk.example.com.",
    "https://clerk.example.com\\evil", "https://clerk.example.com\n", None])
def test_config_rejects_unsafe_issuer(issuer):
    with pytest.raises(AuthConfigurationError):
        AuthConfig(issuer)


def test_explicit_environment_factory(monkeypatch):
    monkeypatch.setenv("CLERK_ISSUER", ISSUER)
    with pytest.raises(AuthConfigurationError):
        AuthConfig.from_env({})  # Explicit mapping never falls back to process or files.
    for parties in ["", "null", '"https://app.example"', "{}", "[null]"]:
        with pytest.raises(AuthConfigurationError):
            AuthConfig.from_env({"CLERK_ISSUER": ISSUER, "CLERK_AUTHORIZED_PARTIES": parties})
    config = AuthConfig.from_env({"CLERK_ISSUER": ISSUER, "CLERK_AUTHORIZED_PARTIES": '["https://app.example"]'})
    assert config.authorized_parties == ("https://app.example",)
    with pytest.raises(AuthConfigurationError):
        AuthConfig(ISSUER, audience="")
    assert config.jwks_url == ISSUER + "/.well-known/jwks.json"


async def test_cache_scope_and_lifecycle(keys):
    h = Harness(keys)
    try:
        with pytest.raises(AuthConfigurationError):
            AuthVerifier(AuthConfig("https://another.example.com"), cache=h.cache)
        explicit = verifier_from_env({"CLERK_ISSUER": ISSUER}, cache=h.cache, clock=lambda: NOW)
        assert explicit.config == h.config
        await explicit.start()
        await h.verifier.aclose()
        await failure(h, code="auth_unavailable")
    finally:
        await h.verifier.aclose()
