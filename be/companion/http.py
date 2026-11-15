"""Eight typed domain routes and generated FastAPI documentation; no authentication."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .bootstrap import build_service
from .daily import DailyCoordinator
from .domain import utcnow
from .contracts import (ROOT, DomainError, State, Health, ObservationBatch, IngestionResponse,
    FeedbackRequest, PreferencesRequest, ProcessRequest, DemoRequest, DeleteRequest, ErrorResponse, validate)

REQUESTS = {("POST", "/observations"): "observation_batch", ("POST", "/feedback"): "feedback_request",
            ("PUT", "/preferences"): "preferences_request", ("POST", "/process"): "process_request",
            ("POST", "/demo"): "demo_request", ("DELETE", "/data"): "delete_request"}


def envelope(code, message, request_id=None):
    return {"error": {"code": code, "message": message, "retryable": code in {"busy", "internal_error"},
                      "request_id": request_id, "details": []}}


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def validated_hosts(hosts):
    """Exact DNS/IPv4 names only; no wildcards, ports or proxy-derived authority."""
    hosts = frozenset(hosts)
    if not hosts or any(not isinstance(host, str) or len(host) > 253 or
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", host)
            for host in hosts):
        raise ValueError("API hosts must be explicit lowercase DNS or IPv4 names.")
    return hosts


def allowed_host(scope, hosts):
    values = [value for key, value in scope.get("headers", []) if key.lower() == b"host"]
    if len(values) != 1:
        return False
    match = re.fullmatch(rb"([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?", values[0])
    return bool(match and match[1].decode("ascii").lower() in hosts
                and (match[2] is None or 1 <= int(match[2]) <= 65535))


class Boundary:
    """Bound bytes before buffering; reject malformed/unknown JSON without echoing private inputs."""
    def __init__(self, app, plaid_profile=False, experience_profile=False, banking_profile=False, allowed_hosts=LOOPBACK_HOSTS):
        self.app = app
        self.allowed_hosts = validated_hosts(allowed_hosts)
        self.plaid_profile = plaid_profile
        self.experience_profile = experience_profile
        self.banking_profile = banking_profile

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = None
        response_started = False
        async def guarded_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)
        try:
            headers = dict(scope["headers"])
            if not allowed_host(scope, self.allowed_hosts):
                raise DomainError("invalid_request", 400, "Unrecognized API host.")
            route = (scope["method"], scope["path"])
            requests = dict(REQUESTS)
            if self.plaid_profile:
                requests[("POST", "/connections/plaid")] = "request"
            if self.experience_profile:
                requests[("POST", "/experience/v1/answers")] = "answer_request"
                requests[('POST', '/patterns/v1/analyze')] = 'pattern_request'
                requests[('POST', '/patterns/v1/clarify')] = 'pattern_clarify'
                requests[('POST','/patterns/v1/brief')] = 'pattern_brief'
                requests[('POST','/health/v1/incremental')]='health_incremental'
                requests[('POST','/workflows/v1/enqueue')] = 'workflow_enqueue'
                for action in ('status','register','revoke','logout'):requests[('POST','/report-notifications/v1/'+action)]='notification_request'
                requests[('POST','/health/v1/samples')] = 'health_samples'
            if self.banking_profile:
                requests[('POST','/banking/v1/actions')] = 'banking_request'
            reads = {"/health", "/state"} | ({"/experience/v1", '/patterns/v1', '/workflows/v1','/report-notifications/v1','/health/v1/incremental/status'} if self.experience_profile else set()) | ({'/banking/v1'} if self.banking_profile else set())
            if scope.get("query_string") and scope["path"] in {*reads, *(r[1] for r in requests)}:
                raise DomainError()
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > 1024 * 1024:
                    raise DomainError("invalid_request", 413, "Request body exceeds 1 MiB.")
                chunks.append(chunk)
                if not message.get("more_body"):
                    break
            raw = b"".join(chunks)
            if route in requests:
                try:
                    def no_duplicates(pairs):
                        result = {}
                        for k, v in pairs:
                            if k in result:
                                raise ValueError("Duplicate property")
                            result[k] = v
                        return result
                    body = json.loads(raw, object_pairs_hook=no_duplicates,
                                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeError, RecursionError):
                    raise DomainError("invalid_request", 400, "Malformed JSON body.") from None
                if isinstance(body, dict) and isinstance(body.get("request_id"), str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", body["request_id"]):
                    request_id = body["request_id"]
                if headers.get(b"content-type", b"").split(b";")[0].strip().lower() != b"application/json":
                    raise DomainError()
                if route == ('POST','/health/v1/samples'):
                    from .health_ingestion import HealthIngestRequest
                    try: HealthIngestRequest.model_validate_json(raw)
                    except Exception: raise DomainError() from None
                elif route == ('POST','/health/v1/incremental'):
                    from .health_incremental import parse_request
                    parse_request(body)
                elif route[1].startswith('/report-notifications/v1/'):
                    from .report_notification_contracts import NotificationRegister,NotificationRevoke,NotificationLogout,NotificationStatusRequest
                    try: {'status':NotificationStatusRequest,'register':NotificationRegister,'revoke':NotificationRevoke,'logout':NotificationLogout}[route[1].split('/')[-1]].model_validate(body)
                    except Exception:raise DomainError() from None
                elif route == ('POST','/workflows/v1/enqueue'):
                    from .workflow_contracts import EnqueueRequest
                    try:EnqueueRequest.model_validate(body)
                    except Exception:raise DomainError() from None
                elif route in {('POST','/patterns/v1/analyze'),('POST','/patterns/v1/clarify'),('POST','/patterns/v1/brief')}:
                    from .patterns import AnalyzeRequest, ClarifyRequest, BriefRequest
                    try: ({'/patterns/v1/analyze':AnalyzeRequest,'/patterns/v1/clarify':ClarifyRequest,'/patterns/v1/brief':BriefRequest}[route[1]]).model_validate(body)
                    except Exception: raise DomainError() from None
                elif route == ('POST','/banking/v1/actions'):
                    from .banking_contracts import validate_banking
                    validate_banking('request',body)
                elif route == ("POST", "/connections/plaid"):
                    from .plaid_contracts import validate_profile
                    validate_profile("request", body)
                elif route == ("POST", "/experience/v1/answers"):
                    from .experience_contracts import validate_experience
                    validate_experience("answer_request", body)
                else:
                    validate(requests[route], body)
            elif scope["path"] in reads and raw:
                raise DomainError()
            scope.setdefault("state", {})["request_id"] = request_id
            delivered = False
            async def replay_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": raw}
                return await receive()
            await self.app(scope, replay_receive, guarded_send)
        except DomainError as exc:
            if not response_started:
                await JSONResponse(envelope(exc.code, exc.message, request_id), status_code=exc.status)(scope, receive, send)
        except Exception:
            # Catch before the server's outer error middleware can log a private traceback.
            if not response_started:
                await JSONResponse(envelope("internal_error", "Local operation failed; no private error details are exposed.", request_id), status_code=500)(scope, receive, send)


def create_app(data_dir: Path | None = None, *, replacements=None, scheduler=None, clock=None,
               synthesis_timeout=30, housekeeping_interval=60, plaid_profile=False, experience_profile=False,
               webhook_url=None, account_id=None, plaid_owner_check=None, banking_profile=False, banking_claim_item=None,
               allowed_hosts=LOOPBACK_HOSTS):
    allowed_hosts = validated_hosts(allowed_hosts)
    plaid_profile = plaid_profile or banking_profile
    service = build_service(data_dir or ROOT / "be" / ".data", replacements, scheduler, clock, synthesis_timeout, plaid_profile, experience_profile, webhook_url, account_id, plaid_owner_check, banking_profile, banking_claim_item)
    state_model, ingestion_model = State, IngestionResponse
    if plaid_profile:
        from .plaid_contracts import PlaidState, PlaidIngestionResponse
        state_model, ingestion_model = PlaidState, PlaidIngestionResponse
    daily = DailyCoordinator(service, clock=clock or utcnow)
    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        stopping = asyncio.Event()
        async def housekeeping():
            while not stopping.is_set():
                try:
                    await service.housekeep()
                    if service.plaid_coordinator:
                        await service.plaid_coordinator.daily_sync()
                    await daily.tick()
                except Exception:
                    # No private records or exception payloads in ordinary logs.
                    app.state.housekeeping_failed = True
                # Processing records cancellation as a failed run; do not resume
                # sleeping if that swallowed the worker's shutdown cancellation.
                if stopping.is_set():
                    return
                await asyncio.sleep(housekeeping_interval)
        worker = asyncio.create_task(housekeeping())
        try:
            yield
        finally:
            stopping.set()
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            await service.close()
    app = FastAPI(title="Local Companion", version="1", lifespan=lifespan,
                  redoc_url=None, swagger_ui_oauth2_redirect_url=None)
    app.state.service = service
    app.state.housekeeping_failed = False
    app.add_middleware(Boundary, plaid_profile=plaid_profile, experience_profile=experience_profile, banking_profile=banking_profile, allowed_hosts=allowed_hosts)
    errors = {status: {"model": ErrorResponse} for status in (400, 404, 409, 413, 422, 500)}

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(envelope(exc.code, exc.message, getattr(request.state, "request_id", None)), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc):
        return JSONResponse(envelope("invalid_request", "Request does not satisfy the v1 contract.", getattr(request.state, "request_id", None)), status_code=422)

    @app.exception_handler(HTTPException)
    async def framework_error(request: Request, exc):
        code = "not_found" if exc.status_code == 404 else "invalid_request"
        return JSONResponse(envelope(code, "Unknown route or unsupported method."), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc):
        return JSONResponse(envelope("internal_error", "Local operation failed; no private error details are exposed.", getattr(request.state, "request_id", None)), status_code=500)

    @app.get("/health", response_model=Health, responses=errors)
    async def health():
        return {"schema_version": "1", "status": "degraded" if app.state.housekeeping_failed or service.required_failure else "ok",
                "service": "local-companion", "capabilities": [c for c in service.registry.metadata() if c["id"] not in {'experience','banking'}]}

    @app.get("/state", response_model=state_model, responses=errors)
    async def state():
        return await service.state()

    @app.post("/observations", response_model=ingestion_model, responses=errors)
    async def observations(body: ObservationBatch):
        return await service.mutate("/observations", body.model_dump())

    @app.post("/feedback", response_model=state_model, responses=errors)
    async def feedback(body: FeedbackRequest):
        return await service.mutate("/feedback", body.model_dump())

    @app.put("/preferences", response_model=state_model, responses=errors)
    async def preferences(body: PreferencesRequest):
        return await service.mutate("/preferences", body.model_dump())

    @app.post("/process", response_model=state_model, responses=errors)
    async def process(body: ProcessRequest):
        if getattr(service,'workflow_queue',None) is not None and body.synthesis_mode!='disabled':raise DomainError('consent_required',403,'AI review runs only through the queued workflow.')
        return await service.mutate("/process", body.model_dump())

    @app.post("/demo", response_model=state_model, responses=errors)
    async def demo(body: DemoRequest):
        return await service.mutate("/demo", body.model_dump())

    @app.delete("/data", response_model=state_model, responses=errors)
    async def delete(body: DeleteRequest):
        return await service.mutate("/data", body.model_dump())

    if plaid_profile:
        from .plaid_contracts import PlaidRequest, PlaidResponse
        @app.post("/connections/plaid", response_model=PlaidResponse, responses=errors)
        async def plaid(body: PlaidRequest):
            return await service.mutate("/connections/plaid", body.model_dump())

    if experience_profile:
        from .experience_contracts import Experience, AnswerRequest, ExperienceError
        experience_errors = {status: {"model": ExperienceError} for status in errors}

        from .patterns import AnalyzeRequest, ClarifyRequest, BriefRequest, PatternOverview
        from .health_ingestion import HealthIngestRequest, ingest_health

        @app.post('/health/v1/samples',response_model=state_model,responses=errors)
        async def health_samples(body: HealthIngestRequest):
            return await ingest_health(service,body.model_dump(mode='json'))

        def patterns():
            coordinator = getattr(service, 'pattern_coordinator', None)
            if coordinator is None:
                raise DomainError('invalid_request', 503, 'Normalized PostgreSQL pattern storage is unavailable.')
            return coordinator

        @app.get('/patterns/v1', response_model=PatternOverview, responses=experience_errors)
        async def pattern_overview():
            return await patterns().get()

        @app.post('/patterns/v1/analyze', response_model=PatternOverview, responses=experience_errors)
        async def analyze_patterns(body: AnalyzeRequest):
            raise DomainError('consent_required',403,'Direct manual analysis is disabled. Use the authorized workflow action.')

        from .health_incremental import HealthIncrementalRequest,HealthIncrementalReceipt,ingest_health_incremental
        from .health_incremental_service import HealthIncrementalStatus

        def incremental():
            value=getattr(service,'incremental_health',None)
            if value is None:raise DomainError('not_configured',409)
            return value

        @app.get('/health/v1/incremental/status',response_model=HealthIncrementalStatus,responses=experience_errors)
        async def health_incremental_status():
            return await incremental().status(service)

        @app.post('/health/v1/incremental',response_model=HealthIncrementalReceipt,responses=experience_errors)
        async def health_incremental_ingest(body: HealthIncrementalRequest):
            await incremental().require(service)
            return await ingest_health_incremental(service,body)

        from .report_notification_contracts import NotificationStatus,NotificationRegister,NotificationRegistration,NotificationRevoke,NotificationLogout,NotificationLogoutResult,NotificationStatusRequest
        from .auth import Principal

        def notification_context(request):
            principal=request.scope.get('state',{}).get('principal')
            if not isinstance(principal,Principal):raise DomainError('consent_required',403)
            notifications=getattr(service,'report_notifications',None)
            if notifications is None:raise DomainError('not_configured',409)
            return notifications,principal

        @app.get('/report-notifications/v1',response_model=NotificationStatus,responses=experience_errors)
        async def report_notification_status(request: Request):
            notifications,principal=notification_context(request)
            return await notifications.status(principal,service.document)

        @app.post('/report-notifications/v1/status',response_model=NotificationStatus,responses=experience_errors)
        async def report_notification_selected_status(body: NotificationStatusRequest,request: Request):
            notifications,principal=notification_context(request)
            return await notifications.status(principal,service.document,body.registration_request_id)

        @app.post('/report-notifications/v1/register',response_model=NotificationRegistration,responses=experience_errors)
        async def report_notification_register(body: NotificationRegister,request: Request):
            notifications,principal=notification_context(request)
            return await notifications.register(principal,body,service.document)

        @app.post('/report-notifications/v1/revoke',response_model=NotificationStatus,responses=experience_errors)
        async def report_notification_revoke(body: NotificationRevoke,request: Request):
            notifications,principal=notification_context(request)
            return await notifications.revoke(principal,body,service.document)

        @app.post('/report-notifications/v1/logout',response_model=NotificationLogoutResult,responses=experience_errors)
        async def report_notification_logout(body: NotificationLogout,request: Request):
            notifications,principal=notification_context(request)
            return await notifications.logout(principal)

        from .workflow_contracts import WorkflowOverview,EnqueueRequest

        def workflows():
            queue=getattr(service,'workflow_queue',None)
            if queue is None:raise DomainError('not_configured',409)
            return queue

        @app.get('/workflows/v1',response_model=WorkflowOverview,responses=experience_errors)
        async def workflow_overview():
            return await workflows().view(service.workflow_account,service)

        @app.post('/workflows/v1/enqueue',response_model=WorkflowOverview,responses=experience_errors)
        async def workflow_enqueue(body: EnqueueRequest):
            await workflows().enqueue(service.workflow_account,body.request_id,body.data_epoch,'self')
            return await workflows().view(service.workflow_account,service)

        @app.post('/patterns/v1/clarify', response_model=PatternOverview, responses=experience_errors)
        async def clarify_pattern(body: ClarifyRequest):
            return await patterns().clarify(body)

        @app.post('/patterns/v1/brief',response_model=PatternOverview,responses=experience_errors)
        async def save_pattern_brief(body: BriefRequest):
            return await patterns().save_brief(body)

        @app.get("/experience/v1", response_model=Experience, responses=experience_errors)
        async def experience():
            return await service.experience_coordinator.get()

        @app.post("/experience/v1/answers", response_model=Experience, responses=experience_errors)
        async def answer(body: AnswerRequest):
            return await service.experience_coordinator.answer(body.model_dump())

    if banking_profile:
        from .banking_contracts import Banking, BankingRequest, BankingError
        banking_errors = {status: {'model': BankingError} for status in errors}
        @app.get('/banking/v1',response_model=Banking,responses=banking_errors)
        async def banking():
            return await service.banking_coordinator.get()
        @app.post('/banking/v1/actions',response_model=Banking,responses=banking_errors)
        async def banking_action(body: BankingRequest):
            return await service.banking_coordinator.action(body.model_dump())

    return app
