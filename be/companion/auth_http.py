"""Explicit single-worker auth profile; existing domain HTTP logic stays per-user.

No environment/dotenv reads, socket binding, global service, or anonymous fallback.
TLS termination is the launcher's responsibility. Hosts default to loopback;
container composition may supply an explicit validated public API host.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
import inspect
from pathlib import Path
from weakref import WeakSet

from fastapi import FastAPI
from starlette.responses import JSONResponse

from .accounts import AccountError, AccountManager
from .auth import AuthConfig, AuthConfigurationError, AuthError, AuthVerifier
from .http import create_app, LOOPBACK_HOSTS, validated_hosts, allowed_host


_CACHE_HEADERS = {"Cache-Control": "no-store", "Vary": "Authorization"}
_MESSAGES = {
    "unauthorized": "Authentication required.",
    "session_pending": "Session is not active.",
    "auth_unavailable": "Authentication is temporarily unavailable.",
    "accounts_unavailable": "Account service is temporarily unavailable.",
    "not_found": "Unknown route or unsupported method.",
    "invalid_request": "Request does not satisfy the HTTP contract.",
    "internal_error": "Local operation failed; no private error details are exposed.",
}


def _error(code: str, status: int) -> JSONResponse:
    headers = dict(_CACHE_HEADERS)
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse({"error": {"code": code, "message": _MESSAGES[code],
        "retryable": status in {500, 503}, "request_id": None, "details": []}},
        status_code=status, headers=headers)


class _ChildService:
    """AccountManager starts this wrapper, never the underlying service twice."""

    def __init__(self, app: FastAPI):
        self.app = app
        self._context = app.router.lifespan_context(app)
        self._entered = False
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise AccountError()
        if self._entered:
            return
        try:
            await self._context.__aenter__()
        except BaseException:
            # create_app starts the service before its lifespan's try/finally.
            # If startup fails, release any partially opened extensions as well.
            await self.close()
            raise
        self._entered = True

    async def close(self) -> None:
        if self._closed:
            return
        if self._entered:
            await self._context.__aexit__(None, None, None)
        else:
            await self.app.state.service.close()
        self._closed = True


class _Gateway:
    def __init__(self, accounts: AccountManager, verifier: AuthVerifier, *,
                 plaid_profile: bool, experience_profile: bool, webhook_receiver=None, banking_profile=False,
                 allowed_hosts=LOOPBACK_HOSTS):
        self.allowed_hosts = validated_hosts(allowed_hosts)
        self.accounts, self.verifier = accounts, verifier
        self.webhook_receiver = webhook_receiver
        self.routes = {("GET", "/state"), ("POST", "/observations"),
            ("POST", "/feedback"), ("PUT", "/preferences"),
            ("POST", "/process"), ("DELETE", "/data")}
        if plaid_profile:
            self.routes.add(("POST", "/connections/plaid"))
        if experience_profile:
            self.routes.update({("GET", "/experience/v1"), ("POST", "/experience/v1/answers"),
                ('POST','/health/v1/samples'), ('GET','/patterns/v1'), ('POST','/patterns/v1/analyze'), ('POST','/patterns/v1/clarify'), ('POST','/patterns/v1/brief')})
        if banking_profile:
            self.routes.update({('GET','/banking/v1'),('POST','/banking/v1/actions')})
        if webhook_receiver is not None:
            self.routes.add(('POST', '/webhooks/plaid'))
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closing = False
        self._close_task: asyncio.Task | None = None

    async def close(self) -> None:
        # Stop admissions before draining. Keep cleanup alive if the caller is
        # cancelled, and wait for it before propagating cancellation.
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._drain())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await self._close_task
            raise

    async def _drain(self) -> None:
        await self._idle.wait()
        try:
            await self.accounts.aclose()
        finally:
            await self.verifier.aclose()

    @staticmethod
    def _canonical(scope) -> bool:
        # No redirect_slashes, encoded separators, dot/percent aliases, mounts,
        # or decoded-path differences can expose child docs/demo/health routes.
        path = scope.get("path", "")
        try:
            return (not scope.get("root_path")
                    and scope.get("raw_path", path.encode("ascii")) == path.encode("ascii"))
        except UnicodeError:
            return False

    async def handle(self, scope, receive, send) -> None:
        route = (scope["method"], scope["path"])
        if not self._canonical(scope) or route not in self.routes | {
                ("GET", "/health"), ("GET", "/auth/v1/session")}:
            await _error("not_found", 404)(scope, receive, send)
            return
        if not allowed_host(scope, self.allowed_hosts):
            await _error("invalid_request", 400)(scope, receive, send)
            return
        if route == ("GET", "/health"):
            if scope.get("query_string"):
                await _error("invalid_request", 400)(scope, receive, send)
            else:
                await JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        if self._closing:
            await _error("auth_unavailable", 503)(scope, receive, send)
            return
        # There is no await between checking closing and acquiring the lease.
        # It spans authentication, admission, all child awaits and response send.
        self._active += 1
        self._idle.clear()
        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in {b"cache-control", b"vary"}]
                vary = [part.strip() for k, v in message.get("headers", []) if k.lower() == b"vary"
                        for part in v.split(b",") if part.strip().lower() != b"authorization"]
                headers.extend([(b"cache-control", b"no-store"),
                                (b"vary", b", ".join([*vary, b"Authorization"]))])
                message = {**message, "headers": headers}
            await send(message)

        try:
            if route == ('POST', '/webhooks/plaid'):
                await self.webhook_receiver(scope, receive, safe_send)
                return
            # Preserve raw duplicate headers for the real verifier. No body read,
            # account mapping, child construction or vendor effect precedes this.
            try:
                principal = await self.verifier.authenticate_request(scope.get("headers", []))
            except AuthError:
                raise
            except Exception:
                raise AuthError("auth_unavailable") from None
            if scope.get("query_string"):
                await _error("invalid_request", 400)(scope, receive, safe_send)
                return
            if route == ("GET", "/auth/v1/session"):
                # Strict no-body handshake, without touching the account registry.
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if message.get("body"):
                        await _error("invalid_request", 400)(scope, receive, safe_send)
                        return
                    if not message.get("more_body"):
                        break
                await JSONResponse({"schema_version": "1", "issuer": principal.issuer,
                    "subject": principal.subject}, headers=_CACHE_HEADERS)(scope, receive, safe_send)
                return
            handle = await self.accounts.service_for(principal)
            # Identity is server-owned scope metadata only, never a synthetic
            # client header. Do not pass bearer/cookies or caller-selected owners.
            child_scope = {**scope, "state": {"principal": principal, "account_id": handle.account_id},
                "headers": [(k, v) for k, v in scope.get("headers", [])
                            if k.lower() in {b"host", b"content-type", b"content-length", b"accept", b"accept-encoding"}]}
            await handle.service.app(child_scope, receive, safe_send)
        except AuthError as exc:
            if not started:
                await _error(exc.code, exc.status_code)(scope, receive, safe_send)
        except AccountError:
            if not started:
                await _error("accounts_unavailable", 503)(scope, receive, safe_send)
        except Exception:
            # No framework traceback or exception text may disclose credentials.
            if not started:
                await _error("internal_error", 500)(scope, receive, safe_send)
        finally:
            self._active -= 1
            if not self._active:
                self._idle.set()


class _Boundary:
    def __init__(self, app, *, gateway: _Gateway):
        self.app, self.gateway = app, gateway

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            await self.gateway.handle(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def create_authenticated_app(auth_root: Path, *, auth_config: AuthConfig | None = None,
        verifier: AuthVerifier | None = None,
        service_factory: Callable[[Path], FastAPI | Awaitable[FastAPI]] | None = None,
        clock=None, synthesis_timeout=30, housekeeping_interval=60,
        plaid_profile=False, experience_profile=False, max_accounts=128,
        postgres_runtime=None, webhook_url=None, webhook_verifier=None,
        replacements_factory=None, webhook_worker_enabled=True, banking_profile=False,
        allowed_hosts=LOOPBACK_HOSTS) -> FastAPI:
    """Build an explicitly authenticated app without initializing user storage.

    Supply auth_config or a configured verifier (both must agree if supplied).
    No settings are loaded implicitly. JWKS is lazy: outage gives protected 503,
    not anonymous fallback. The app owns and closes even an injected verifier.

    service_factory(path) is a trusted per-account CHILD APP factory, normally a
    closure calling http.create_app(path, replacements=fresh_replacements, ...).
    It may be async and must return a fresh app/service with its own lifespan,
    clock/scheduler/coordinators and only that directory's stores. When supplied,
    it owns child configuration; match the profile flags and allowed_hosts passed here. Otherwise
    create_app receives clock/timeouts/profiles directly. Never reuse mutable
    replacements across identities. state.accounts exposes internal handles for
    server-owned work/tests, not an HTTP identity-selection interface.
    """
    allowed_hosts = validated_hosts(allowed_hosts)
    plaid_profile = plaid_profile or banking_profile
    if auth_config is not None and not isinstance(auth_config, AuthConfig):
        raise AuthConfigurationError("Invalid Clerk authentication configuration")
    if verifier is None:
        if auth_config is None:
            raise AuthConfigurationError("Explicit Clerk authentication configuration is required")
        verifier = AuthVerifier(auth_config)
    elif (not isinstance(getattr(verifier, "config", None), AuthConfig)
          or auth_config is not None and verifier.config != auth_config):
        raise AuthConfigurationError("Verifier configuration mismatch")

    if postgres_runtime is not None and service_factory is not None:
        raise ValueError('PostgreSQL composition owns the persistence factory.')
    if webhook_url and (postgres_runtime is None or not plaid_profile):
        raise ValueError('Webhooks require PostgreSQL and the Sandbox profile.')
    children, services = WeakSet(), WeakSet()

    async def child_service(path: Path):
        if service_factory is None:
            replacements = replacements_factory(path) if replacements_factory else {}
            if postgres_runtime is not None:
                from .extensions.postgres import PostgresPersistenceExtension
                replacements = {**replacements, 'persistence': PostgresPersistenceExtension(postgres_runtime, path.name, residual_dir=path)}
            child = create_app(path, clock=clock, synthesis_timeout=synthesis_timeout, allowed_hosts=allowed_hosts,
                housekeeping_interval=housekeeping_interval, plaid_profile=plaid_profile,
                experience_profile=experience_profile, replacements=replacements,
                webhook_url=webhook_url, account_id=path.name,
                plaid_owner_check=(lambda item: postgres_runtime.may_remove_item(path.name,item)) if postgres_runtime else None,
                banking_profile=banking_profile,
                banking_claim_item=(lambda item,epoch: postgres_runtime.reserve_item(path.name,item,epoch)) if postgres_runtime else None)
        else:
            child = service_factory(path)
            if inspect.isawaitable(child):
                child = await child
        if postgres_runtime is not None and experience_profile:
            from .pattern_store import PatternCoordinator
            child.state.service.pattern_coordinator = PatternCoordinator(child.state.service, replacements['persistence'])
        # Reject accidentally shared application/service before starting it.
        if child in children or child.state.service in services:
            raise AccountError()
        children.add(child)
        services.add(child.state.service)
        return _ChildService(child)

    if postgres_runtime is None:
        accounts = AccountManager(auth_root, child_service, max_accounts=max_accounts)
    else:
        from .postgres_accounts import PostgresAccountManager
        accounts = PostgresAccountManager(auth_root, child_service, postgres_runtime,
            max_accounts=min(max_accounts, 8, max(1, postgres_runtime.config.max_connections//2)))
    receiver, worker, inbox = None, None, None
    if webhook_url:
        from .plaid_webhooks import PlaidWebhookVerifier
        from .webhook_http import SandboxVerificationKeys, WebhookReceiver
        from .webhook_jobs import WebhookInbox, WebhookWorker
        from .connections.plaid import PlaidSandboxClient, SandboxCredentials
        inbox = WebhookInbox(postgres_runtime)
        webhook_verifier = webhook_verifier or PlaidWebhookVerifier(SandboxVerificationKeys(PlaidSandboxClient(SandboxCredentials.from_environment())))
        receiver = WebhookReceiver(webhook_verifier, inbox)
        async def sync_job(job):
            handle = await accounts.service_for_account(job['account_id'])
            service = handle.service.app.state.service
            if service.banking_coordinator is not None:
                async def bank_guard():
                    return bool(service.document['state']['data_epoch']==job['epoch'] and await inbox.current(job))
                response = await service.banking_coordinator.sync_item(job['item_id'],admission_guard=bank_guard)
                if response and response.get('deferred_until'):return response
                if response and response['result']['status']=='partial':raise AccountError()
                return response
            async with service.lock:
                doc = service.document
                if (doc['state']['data_epoch'] != job['epoch'] or not doc['meta'].get('plaid_connected')
                        or doc['meta'].get('plaid_item_id') != job['item_id'] or not await inbox.current(job)):
                    return
            async def admission_guard():
                meta = service.document['meta']
                return bool(meta.get('plaid_connected') and meta.get('plaid_item_id') == job['item_id'] and await inbox.current(job))
            from uuid import uuid4
            result = await service.plaid_coordinator.request({'schema_version':'1', 'request_id':'webhook:'+str(uuid4()),
                'data_epoch':job['epoch'], 'action':'sync'}, admission_guard=admission_guard,automatic=True)
            if result.get('deferred_until'):return result
            if result['result']['status'] == 'error':
                raise AccountError()
        worker = WebhookWorker(inbox, sync_job, periodic=True)
    gateway = _Gateway(accounts, verifier, plaid_profile=plaid_profile, experience_profile=experience_profile, webhook_receiver=receiver, banking_profile=banking_profile, allowed_hosts=allowed_hosts)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if postgres_runtime is not None:
                await postgres_runtime.check()
            if worker is not None and webhook_worker_enabled:
                await worker.start()
            yield
        finally:
            gateway._closing = True
            if worker is not None:
                await worker.close()
                await webhook_verifier.aclose()
            await gateway.close()
            if postgres_runtime is not None:
                await postgres_runtime.close()

    app = FastAPI(title="Authenticated Companion", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.accounts, app.state.verifier = accounts, verifier
    app.state.webhook_inbox, app.state.webhook_worker = inbox, worker
    app.add_middleware(_Boundary, gateway=gateway)
    return app
