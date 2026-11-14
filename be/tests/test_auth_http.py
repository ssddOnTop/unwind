"""Offline auth-profile integration: scratch signatures, mock JWKS, real user stores."""
import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from companion.auth import AuthConfig, AuthConfigurationError, AuthVerifier, JWKSCache
from companion.auth_http import create_authenticated_app
from companion.bootstrap import UnavailableSynthesis
from companion.contracts import BATCH
from companion.http import create_app

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
ISSUER = "https://clerk.example.com"
EPOCH = "11111111-1111-4111-8111-111111111111"
ROUTES = [("GET", "/auth/v1/session"), ("GET", "/state"), ("POST", "/observations"),
          ("POST", "/feedback"), ("PUT", "/preferences"), ("POST", "/process"),
          ("DELETE", "/data"), ("POST", "/connections/plaid"),
          ("GET", "/experience/v1"), ("POST", "/experience/v1/answers")]


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Stream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


class Harness:
    def __init__(self, root, key, *, plaid_profile=False, experience_profile=False, **options):
        self.root, self.key = root, key
        self.children, self.providers, self.scopes = [], [], []
        self.jwks_calls = 0
        self.outage = False
        self.mono = 1000.0
        self.starts, self.closes, self.housekeeping = [], [], []
        self.config = AuthConfig(ISSUER)
        self.verifier = AuthVerifier(self.config, clock=lambda: NOW.timestamp(),
            cache=JWKSCache(self.config, transport=httpx.MockTransport(self.fetch),
                           resolver=self.resolve, monotonic=lambda: self.mono))
        self.plaid_profile, self.experience_profile = plaid_profile, experience_profile
        self.app = create_authenticated_app(root, verifier=self.verifier,
            service_factory=options.pop("service_factory", self.child),
            plaid_profile=plaid_profile, experience_profile=experience_profile, **options)

    async def resolve(self, host, port):
        assert host == "clerk.example.com" and port == 443
        return ["93.184.216.34"]

    async def fetch(self, request):
        self.jwks_calls += 1
        assert request.url.host == "93.184.216.34"
        assert "authorization" not in request.headers
        if self.outage:
            raise httpx.ConnectError("PRIVATE INFRASTRUCTURE DETAIL")
        n = self.key.public_key().public_numbers()
        key = {"kty": "RSA", "kid": "scratch", "alg": "RS256", "use": "sig",
               "n": b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")),
               "e": b64(n.e.to_bytes((n.e.bit_length() + 7) // 8, "big"))}
        return httpx.Response(200, stream=Stream(json.dumps({"keys": [key]}).encode()))

    def token(self, subject="a", **updates):
        claims = {"iss": ISSUER, "sub": subject, "sid": "session-" + subject,
                  "v": 2, "iat": int(NOW.timestamp()) - 20,
                  "nbf": int(NOW.timestamp()) - 20, "exp": int(NOW.timestamp()) + 60, **updates}
        message = (b64(b'{"alg":"RS256","kid":"scratch"}') + "." + b64(json.dumps(claims).encode())).encode()
        return message.decode() + "." + b64(self.key.sign(message, padding.PKCS1v15(), hashes.SHA256()))

    def headers(self, subject="a", **updates):
        return {"Authorization": "Bearer " + self.token(subject, **updates)}

    def child(self, path):
        replacements = {"synthesis": UnavailableSynthesis(path / "checkpoints.sqlite")}
        if self.plaid_profile:
            from companion.connections.plaid import PlaidSandboxClient, SandboxCredentials
            from companion.extensions.plaid import PlaidExtension
            from test_plaid import Provider
            provider = Provider()
            self.providers.append(provider)
            replacements["plaid"] = PlaidExtension(path, PlaidSandboxClient(
                SandboxCredentials("synthetic-client", "synthetic-secret"), httpx.MockTransport(provider.handle)))
        child = create_app(path, replacements=replacements, clock=lambda: NOW,
            housekeeping_interval=3600, plaid_profile=self.plaid_profile,
            experience_profile=self.experience_profile)
        service = child.state.service
        original_start, original_close, original_housekeep = service.start, service.close, service.housekeep

        async def start():
            self.starts.append(path)
            await original_start()

        async def close():
            self.closes.append(path)
            await original_close()

        async def housekeep():
            self.housekeeping.append(path)
            await original_housekeep()

        service.start, service.close, service.housekeep = start, close, housekeep
        scopes = self.scopes

        class Capture:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http":
                    scopes.append({"headers": list(scope["headers"]), "state": dict(scope.get("state", {}))})
                await self.app(scope, receive, send)

        child.add_middleware(Capture)
        self.children.append(child)
        return child

    @asynccontextmanager
    async def client(self):
        async with self.app.router.lifespan_context(self.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://127.0.0.1") as client:
                yield client

    async def handle(self, subject):
        principal = await self.verifier.verify_token(self.token(subject))
        return await self.app.state.accounts.service_for(principal)


async def raw_call(app, method, path, headers=(), raw_path=None, query=b"", receive=None):
    messages = []

    async def no_body_read():
        raise AssertionError("Untrusted request body was read")

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path,
             "raw_path": raw_path if raw_path is not None else path.encode(),
             "query_string": query, "root_path": "", "server": ("127.0.0.1", 80),
             "client": ("127.0.0.1", 1234), "headers": [(b"host", b"127.0.0.1"), *headers]}
    await app(scope, receive or no_body_read, send)
    status = messages[0]["status"]
    body = json.loads(b"".join(m.get("body", b"") for m in messages))
    return status, body, dict(messages[0]["headers"])


def assert_error(response, code, status):
    assert response.status_code == status, response.text
    value = response.json()
    assert set(value) == {"error"}
    assert set(value["error"]) == {"code", "message", "retryable", "request_id", "details"}
    assert value["error"]["code"] == code
    assert value["error"]["retryable"] == (status == 503)
    assert value["error"]["request_id"] is None and value["error"]["details"] == []
    assert response.headers["cache-control"] == "no-store"
    assert "Authorization" in response.headers["vary"]
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"


async def test_config_must_be_explicit_and_consistent(tmp_path, key, monkeypatch):
    monkeypatch.setenv("CLERK_ISSUER", ISSUER)  # Even ambient config cannot select the profile.
    with pytest.raises(AuthConfigurationError):
        create_authenticated_app(tmp_path / "new")
    with pytest.raises(AuthConfigurationError):
        create_authenticated_app(tmp_path / "new", auth_config={"issuer": ISSUER})
    h = Harness(tmp_path / "root", key)
    with pytest.raises(AuthConfigurationError):
        create_authenticated_app(tmp_path / "new", verifier=h.verifier, auth_config=AuthConfig("https://other.example.com"))
    await h.verifier.aclose()
    assert not list(tmp_path.iterdir())


async def test_health_and_handshake_never_initialize_storage(tmp_path, key):
    h = Harness(tmp_path / "root", key, plaid_profile=True, experience_profile=True)
    async with h.client() as c:
        assert (await c.get("/health")).json() == {"status": "ok"}
        assert h.jwks_calls == 0
        for subject in ("a", "b"):
            response = await c.get("/auth/v1/session", headers=h.headers(subject))
            assert response.status_code == 200
            assert response.json() == {"schema_version": "1", "issuer": ISSUER, "subject": subject}
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["vary"] == "Authorization"
            assert response.headers["content-type"] == "application/json"
        assert not h.children and not h.root.exists()
        assert await h.app.state.accounts.handles() == ()
    assert h.verifier.cache._closed
    assert not h.root.exists()


@pytest.mark.parametrize("method,path", ROUTES)
async def test_missing_and_bad_credentials_read_no_body_create_no_files(tmp_path, key, method, path):
    h = Harness(tmp_path / "root", key, plaid_profile=True, experience_profile=True)
    async with h.client():
        for headers in ([], [(b"authorization", b"Bearer not-a-jwt")],
                        [(b"cookie", b"__session=ignored"), (b"x-user-id", b"b")],
                        [(b"authorization", b"Bearer x"), (b"Authorization", b"Bearer y")]):
            status, body, response_headers = await raw_call(h.app, method, path, headers)
            assert status == 401
            assert body["error"]["code"] == "unauthorized"
            assert response_headers[b"www-authenticate"] == b"Bearer"
        assert h.jwks_calls == 0
        assert not h.root.exists() and not h.children and not h.providers


@pytest.mark.parametrize("claims,code,status", [
    ({"iss": "https://wrong.example.com"}, "unauthorized", 401),
    ({"exp": int(NOW.timestamp()) - 10}, "unauthorized", 401),
    ({"sub": ""}, "unauthorized", 401),
    ({"azp": "untrusted"}, "unauthorized", 401),
    ({"aud": "unconfigured"}, "unauthorized", 401),
    ({"sts": "pending"}, "session_pending", 403),
])
async def test_signed_invalid_or_pending_auth_is_sanitized(tmp_path, key, claims, code, status, caplog):
    h = Harness(tmp_path / "root", key)
    async with h.client() as c:
        token = h.token(**claims)
        for method, path in ROUTES[:7]:
            response = await c.request(method, path, headers={"Authorization": "Bearer " + token}, content=b"PRIVATE_BODY")
            assert_error(response, code, status)
            assert token not in response.text and "PRIVATE_BODY" not in response.text
        assert not h.children and not h.root.exists()
    assert token not in caplog.text and "PRIVATE_BODY" not in caplog.text


async def test_jwks_outage_is_retryable_not_anonymous(tmp_path, key):
    h = Harness(tmp_path / "root", key)
    h.outage = True
    async with h.client() as c:
        for path in ("/auth/v1/session", "/state"):
            response = await c.get(path, headers=h.headers())
            assert_error(response, "auth_unavailable", 503)
            assert "PRIVATE" not in response.text
        assert h.jwks_calls == 1 and not h.root.exists()


@pytest.mark.parametrize("path", ["/demo", "/demo/", "/docs", "/docs/", "/docs/oauth2-redirect",
    "/openapi.json", "/redoc", "/health/", "/state/", "/auth/v1/session/", "/unknown",
    "/experience/v1/demo", "/experience/v1", "/experience/v1/answers", "/connections/plaid"])
async def test_absent_disabled_and_alias_routes_never_create_child(tmp_path, key, path):
    h = Harness(tmp_path / "root", key)
    async with h.client() as c:
        for headers in ({}, h.headers()):
            for method in ("GET", "POST"):
                response = await c.request(method, path, headers=headers)
                assert response.status_code == 404, response.text
                assert "location" not in response.headers
        assert not h.children and not h.root.exists() and h.jwks_calls == 0


async def test_raw_paths_no_redirect_bypass_and_wrong_methods(tmp_path, key):
    h = Harness(tmp_path / "root", key)
    async with h.client():
        headers = [(b"authorization", h.headers()["Authorization"].encode())]
        for path, raw_path in [("/state", b"/%73tate"), ("/state", b"/x/../state"),
                               ("/state", b"/%2fstate"), ("/state", b"/state%2f"),
                               ("/demo", b"/%64emo"), ("/auth/v1/session", b"/auth%2fv1/session")]:
            assert (await raw_call(h.app, "GET", path, headers, raw_path))[0] == 404
        for method, path in [("HEAD", "/health"), ("POST", "/health"), ("POST", "/auth/v1/session"),
                             ("OPTIONS", "/state"), ("GET", "/data")]:
            assert (await raw_call(h.app, method, path, headers))[0] == 404
        assert not h.root.exists()


async def test_handshake_rejects_body_query_and_nonloopback_host(tmp_path, key):
    h = Harness(tmp_path / "root", key)
    async with h.client() as c:
        for response in [await c.get("/auth/v1/session?subject=b", headers=h.headers()),
                         await c.request("GET", "/auth/v1/session", headers=h.headers(), content=b"{}"),
                         await c.get("/auth/v1/session", headers={**h.headers(), "Host": "public.example.com"}),
                         await c.get("/health", headers={"Host": "127.0.0.1.evil.example"})]:
            assert response.status_code == 400
        assert not h.root.exists() and not h.children


async def test_legacy_profile_handshake_is_404_not_auth_proof(tmp_path, key):
    h = Harness(tmp_path / "unused", key)
    anonymous = create_app(tmp_path / "legacy", replacements={"synthesis": UnavailableSynthesis()}, clock=lambda: NOW)
    async with anonymous.router.lifespan_context(anonymous):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=anonymous), base_url="http://127.0.0.1") as c:
            assert (await c.get("/health")).status_code == 200
            assert (await c.get("/auth/v1/session", headers=h.headers())).status_code == 404
    await h.verifier.aclose()
    assert not h.root.exists()


async def test_per_user_once_only_lifespans_safe_scope_and_bounded_registry(tmp_path, key):
    h = Harness(tmp_path / "root", key, max_accounts=2)
    async with h.client() as c:
        responses = await asyncio.gather(*[c.get("/state", headers={**h.headers(user), "X-User-ID": "victim", "Cookie": "secret"})
                                          for user in ("a", "b", "a", "b", "a")])
        assert all(r.status_code == 200 for r in responses)
        assert len(h.children) == len(h.starts) == 2
        assert len(set(h.starts)) == 2
        assert set(h.housekeeping) == set(h.starts)
        handles = await h.app.state.accounts.handles()
        assert len(handles) == 2 and handles[0].service.app is not handles[1].service.app
        assert h.children[0].state.service.scheduler is not h.children[1].state.service.scheduler
        for scope in h.scopes:
            assert not {b"authorization", b"x-user-id", b"cookie"} & {k.lower() for k, _ in scope["headers"]}
            assert scope["state"]["principal"].subject in {"a", "b"}
            assert scope["state"]["account_id"] in {handle.account_id for handle in handles}
        assert_error(await c.get("/state", headers=h.headers("c")), "accounts_unavailable", 503)
        assert len(h.children) == 2
    assert sorted(h.closes) == sorted(h.starts)
    assert await h.app.state.accounts.handles() == ()
    assert h.verifier.cache._closed


def mutation(epoch, rid, **extra):
    return {"schema_version": "1", "data_epoch": epoch, "request_id": rid, **extra}


async def seed(h, user):
    """Seed via internal service handle, not an HTTP demo/seed endpoint."""
    handle = await h.handle(user)
    service = handle.service.app.state.service
    async with service.lock:
        document = deepcopy(service.document)
        document["state"]["data_epoch"] = EPOCH
        await service.commit(document, bump=False)
    preferences = deepcopy((await service.state())["preferences"])
    preferences.update(enabled_source_ids=["demo"], movement_restriction=False,
                       food_suggestions=True, activity_suggestions=True)
    await service.mutate("/preferences", mutation(EPOCH, "fixture-consent", preferences=preferences))
    observations = deepcopy(BATCH["observations"])
    for row in observations:
        if row["kind"] == "purchase":
            row["household_attribution"] = "unknown"
    await service.mutate("/observations", mutation(EPOCH, "fixture-rows", observations=observations,
                                                 source_updates=deepcopy(BATCH["source_updates"])))
    return handle


async def test_identical_payloads_all_routes_isolated_answers_and_delete(tmp_path, key):
    h = Harness(tmp_path / "root", key, experience_profile=True)
    async with h.client() as c:
        a, b = await seed(h, "a"), await seed(h, "b")
        assert a.data_dir != b.data_dir
        for user in ("a", "b"):
            headers = h.headers(user)
            state = (await c.get("/state", headers=headers)).json()
            preferences = deepcopy(state["preferences"])
            preferences["no_spending"] = True
            bodies = [
                ("PUT", "/preferences", mutation(EPOCH, "same-prefs", preferences=preferences)),
                ("POST", "/observations", mutation(EPOCH, "same-rows", observations=[], source_updates=[])),
                ("POST", "/feedback", mutation(EPOCH, "same-feedback", card_id="purchase_recurrence", kind="correctness",
                    value="correct", scope="event", event_ref={"observation_id": "demo-purchase-1", "revision": 1},
                    attribution="self", note="")),
                ("POST", "/process", mutation(EPOCH, "same-process", synthesis_mode="disabled")),
            ]
            for method, path, body in bodies:
                response = await c.request(method, path, headers=headers, json=body)
                assert response.status_code == 200, response.text
                assert (await c.request(method, path, headers=headers, json=body)).json() == response.json()
        before_b = (await c.get("/state", headers=h.headers("b"))).json()
        exp_b = (await c.get("/experience/v1", headers=h.headers("b"))).json()
        exp_a = (await c.get("/experience/v1", headers=h.headers("a"))).json()
        assert exp_a["clarification"] is not None
        answer = mutation(EPOCH, "same-answer", expected_revision=exp_a["revision"],
                          clarification_key=exp_a["clarification"]["key"], answer="yes")
        response = await c.post("/experience/v1/answers", headers=h.headers("a"), json=answer)
        assert response.status_code == 200, response.text
        assert (await c.get("/experience/v1", headers=h.headers("b"))).json() == exp_b
        assert (await c.post("/experience/v1/answers", headers=h.headers("b"), json={**answer, "answer": "no"})).status_code == 200
        assert (await c.get("/experience/v1", headers=h.headers("a"))).json()["clarification"]["answer"] == "yes"
        before_b = (await c.get("/state", headers=h.headers("b"))).json()
        exp_b = (await c.get("/experience/v1", headers=h.headers("b"))).json()
        # Caller-supplied owner selectors cannot route into B, even with its epoch.
        forged = mutation(EPOCH, "forged", confirm="delete_all", subject="b", user_id=b.account_id)
        rejected = await c.request("DELETE", "/data", headers=h.headers("a"), json=forged)
        assert rejected.status_code == 422
        b_bytes = {p.name: p.read_bytes() for p in b.data_dir.iterdir() if p.is_file()}
        deletion = mutation(EPOCH, "same-delete", confirm="delete_all")
        deleted = await c.request("DELETE", "/data", headers=h.headers("a"), json=deletion)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["data_epoch"] != EPOCH and deleted.json()["observations"] == []
        assert (await c.request("DELETE", "/data", headers=h.headers("a"), json=deletion)).json() == deleted.json()
        assert (await c.get("/state", headers=h.headers("b"))).json() == before_b
        assert (await c.get("/experience/v1", headers=h.headers("b"))).json() == exp_b
        assert {p.name: p.read_bytes() for p in b.data_dir.iterdir() if p.is_file()} == b_bytes
        stale = await c.post("/experience/v1/answers", headers=h.headers("a"), json=answer)
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "epoch_mismatch"


async def test_plaid_coordinators_vaults_and_same_ids_are_per_user(tmp_path, key):
    h = Harness(tmp_path / "root", key, plaid_profile=True)
    async with h.client() as c:
        a, b = await seed(h, "a"), await seed(h, "b")
        assert a.service.app.state.service.plaid_coordinator is not b.service.app.state.service.plaid_coordinator
        for user in ("a", "b"):
            for action, extra in [("link_token", {"consent_sandbox": True}),
                                  ("exchange", {"public_token": "public-sandbox-synthetic-test-only"}), ("sync", {})]:
                body = mutation(EPOCH, "same-" + action, action=action, **extra)
                response = await c.post("/connections/plaid", headers=h.headers(user), json=body)
                assert response.status_code == 200, response.text
        assert len(h.providers) == 2 and all(p.requests for p in h.providers)
        assert (a.data_dir / "plaid.key").read_bytes() != (b.data_dir / "plaid.key").read_bytes()
        before = (await c.get("/state", headers=h.headers("b"))).json()
        files = {p.name: p.read_bytes() for p in b.data_dir.iterdir() if p.is_file()}
        result = await c.request("DELETE", "/data", headers=h.headers("a"), json=mutation(EPOCH, "delete", confirm="delete_all"))
        assert result.status_code == 200, result.text
        assert (await c.get("/state", headers=h.headers("b"))).json() == before
        assert {p.name: p.read_bytes() for p in b.data_dir.iterdir() if p.is_file()} == files


async def test_legacy_root_is_never_adopted(tmp_path, key):
    root = tmp_path / "legacy"
    root.mkdir()
    sentinel = root / "state.sqlite"
    sentinel.write_bytes(b"legacy-private-data")
    before = (sentinel.read_bytes(), sentinel.stat().st_mode)
    h = Harness(root, key)
    async with h.client() as c:
        assert (await c.get("/auth/v1/session", headers=h.headers())).status_code == 200
        assert_error(await c.get("/state", headers=h.headers()), "accounts_unavailable", 503)
        assert not h.children
    assert (sentinel.read_bytes(), sentinel.stat().st_mode) == before
    assert list(root.iterdir()) == [sentinel]


async def test_inflight_a_response_is_bound_to_a_and_shutdown_drains(tmp_path, key):
    h = Harness(tmp_path / "root", key)
    context = h.app.router.lifespan_context(h.app)
    await context.__aenter__()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1") as c:
        a, b = await seed(h, "a"), await seed(h, "b")
        entered, release = asyncio.Event(), asyncio.Event()
        original = a.service.app.state.service.state

        async def blocked():
            snapshot = await original()
            entered.set()
            await release.wait()
            return snapshot

        a.service.app.state.service.state = blocked
        late = asyncio.create_task(c.get("/state", headers=h.headers("a")))
        await asyncio.wait_for(entered.wait(), 3)
        b_response = await c.get("/state", headers=h.headers("b"))
        assert b_response.status_code == 200
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not closing.done() and h.closes == []
        assert_error(await c.get("/state", headers=h.headers("b")), "auth_unavailable", 503)
        release.set()
        response = await asyncio.wait_for(late, 3)
        assert response.json() == await original()
        await asyncio.wait_for(closing, 3)
        assert len(h.closes) == 2 and h.verifier.cache._closed
        assert h.scopes[-1]["state"]["account_id"] == b.account_id
        assert h.scopes[-2]["state"]["account_id"] == a.account_id


async def test_request_and_shutdown_cancellation_do_not_strand_children(tmp_path, key):
    h = Harness(tmp_path / "root", key)
    context = h.app.router.lifespan_context(h.app)
    await context.__aenter__()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1") as c:
        handle = await h.handle("a")
        entered = asyncio.Event()

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        handle.service.app.state.service.state = blocked
        request = asyncio.create_task(c.get("/state", headers=h.headers()))
        await asyncio.wait_for(entered.wait(), 3)
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(0)
        closing.cancel()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 3)
    assert len(h.closes) == 1 and h.verifier.cache._closed
    assert await h.app.state.accounts.handles() == ()


async def test_bad_signature_and_unexpected_verifier_failure_are_private(tmp_path, key, caplog):
    h = Harness(tmp_path / "root", key)
    async with h.client() as c:
        segments = h.token().split(".")
        signature = bytearray(base64.urlsafe_b64decode(segments[2] + "=" * (-len(segments[2]) % 4)))
        signature[0] ^= 1
        segments[2] = b64(signature)
        token = ".".join(segments)
        assert_error(await c.get("/state", headers={"Authorization": "Bearer " + token}), "unauthorized", 401)

        async def broken(_):
            raise RuntimeError("PRIVATE TOKEN " + token)

        h.verifier.authenticate_request = broken
        response = await c.get("/auth/v1/session", headers=h.headers())
        assert_error(response, "auth_unavailable", 503)
        assert token not in response.text and token not in caplog.text
        assert not h.children and not h.root.exists()


async def test_cancelled_admission_finishes_once_then_shutdown_closes(tmp_path, key):
    entered, release = asyncio.Event(), asyncio.Event()
    h = Harness(tmp_path / "root", key)
    original_factory = h.child

    async def factory(path):
        child = original_factory(path)
        original_start = child.state.service.start

        async def start():
            entered.set()
            await release.wait()
            await original_start()

        child.state.service.start = start
        return child

    # A fresh app is built before any storage/lifespan use; no global monkeypatch.
    h.app = create_authenticated_app(h.root, verifier=h.verifier, service_factory=factory)
    context = h.app.router.lifespan_context(h.app)
    await context.__aenter__()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1") as c:
        request = asyncio.create_task(c.get("/state", headers=h.headers()))
        await asyncio.wait_for(entered.wait(), 3)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not closing.done() and not h.closes
        release.set()
        await asyncio.wait_for(closing, 3)
    assert len(h.starts) == len(h.closes) == 1
    assert h.verifier.cache._closed
    assert await h.app.state.accounts.handles() == ()


async def test_failed_child_startup_is_closed_and_never_published(tmp_path, key, caplog):
    h = Harness(tmp_path / "root", key)

    def factory(path):
        child = h.child(path)
        start = child.state.service.start

        async def broken():
            await start()
            raise RuntimeError("PRIVATE STARTUP FAILURE")

        child.state.service.start = broken
        return child

    h.app = create_authenticated_app(h.root, verifier=h.verifier, service_factory=factory)
    async with h.client() as c:
        response = await c.get("/state", headers=h.headers())
        assert_error(response, "accounts_unavailable", 503)
        assert "PRIVATE" not in response.text and "PRIVATE" not in caplog.text
        assert await h.app.state.accounts.handles() == ()
        assert len(h.starts) == len(h.closes) == 1


async def test_injected_shared_child_is_refused_without_closing_a(tmp_path, key):
    h = Harness(tmp_path / "root", key)

    def factory(path):
        return h.children[0] if h.children else h.child(path)

    h.app = create_authenticated_app(h.root, verifier=h.verifier, service_factory=factory)
    async with h.client() as c:
        assert (await c.get("/state", headers=h.headers("a"))).status_code == 200
        assert_error(await c.get("/state", headers=h.headers("b")), "accounts_unavailable", 503)
        assert not h.closes
        assert (await c.get("/state", headers=h.headers("a"))).status_code == 200
        assert len(h.starts) == 1
    assert len(h.closes) == 1


async def test_session_rotation_restart_and_no_anonymous_data_adoption(tmp_path, key):
    legacy = tmp_path / "anonymous"
    legacy.mkdir()
    (legacy / "state.sqlite").write_bytes(b"PRIVATE LEGACY SENTINEL")
    h = Harness(tmp_path / "root", key)
    async with h.client() as c:
        a = await seed(h, "a")
        before = (await c.get("/state", headers=h.headers())).json()
        assert (await c.get("/state", headers=h.headers(sid="new-session"))).json() == before
        assert len(h.children) == 1
        account_id = a.account_id
    restarted = Harness(tmp_path / "root", key)
    async with restarted.client() as c:
        assert (await c.get("/state", headers=restarted.headers())).json() == before
        assert (await restarted.handle("a")).account_id == account_id
        assert (await c.get("/state", headers=restarted.headers("b"))).json()["observations"] == []
    assert (legacy / "state.sqlite").read_bytes() == b"PRIVATE LEGACY SENTINEL"


async def test_late_synthesis_is_fenced_after_delete_and_cannot_change_b(tmp_path, key):
    from companion.domain import synthesis
    h = Harness(tmp_path / "root", key)
    async with h.client() as c:
        a, b = await seed(h, "a"), await seed(h, "b")
        service = a.service.app.state.service
        dispatch = service.registry.dispatch
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def delayed(capability, operation, payload):
            if capability != "synthesis" or operation != "synthesize":
                return await dispatch(capability, operation, payload)
            assert payload["data_epoch"] == EPOCH
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # A provider can finish after cancellation; epoch fencing, not
                # cancellation alone, must prevent a stale result being stored.
                cancelled.set()
                await release.wait()
            return synthesis(payload["mode"], "skipped", "disabled")

        service.registry.dispatch = delayed
        processing = asyncio.create_task(c.post("/process", headers=h.headers("a"),
            json=mutation(EPOCH, "late-process", synthesis_mode="disabled")))
        await asyncio.wait_for(entered.wait(), 3)
        b_before = (await c.get("/state", headers=h.headers("b"))).json()
        b_files = {p.name: p.read_bytes() for p in b.data_dir.iterdir() if p.is_file()}
        deleting = asyncio.create_task(c.request("DELETE", "/data", headers=h.headers("a"),
            json=mutation(EPOCH, "late-delete", confirm="delete_all")))
        await asyncio.wait_for(cancelled.wait(), 3)
        assert not deleting.done()
        release.set()
        deleted, late = await asyncio.wait_for(asyncio.gather(deleting, processing), 3)
        assert deleted.status_code == late.status_code == 200
        assert deleted.json()["data_epoch"] != EPOCH
        assert late.json() == deleted.json()
        assert (await c.get("/state", headers=h.headers("a"))).json() == deleted.json()
        assert (await c.get("/state", headers=h.headers("b"))).json() == b_before
        assert {p.name: p.read_bytes() for p in b.data_dir.iterdir() if p.is_file()} == b_files
