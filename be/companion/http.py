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


class Boundary:
    """Bound bytes before buffering; reject malformed/unknown JSON without echoing private inputs."""
    def __init__(self, app):
        self.app = app

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
            host = headers.get(b"host", b"").decode().split(":")[0]
            if host not in {"127.0.0.1", "localhost"}:
                raise DomainError("invalid_request", 400, "Only local loopback hosts are supported.")
            route = (scope["method"], scope["path"])
            if scope.get("query_string") and scope["path"] in {"/health", "/state", *(r[1] for r in REQUESTS)}:
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
            if route in REQUESTS:
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
                validate(REQUESTS[route], body)
            elif scope["path"] in {"/health", "/state"} and raw:
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
               synthesis_timeout=30, housekeeping_interval=60):
    service = build_service(data_dir or ROOT / "be" / ".data", replacements, scheduler, clock, synthesis_timeout)
    daily = DailyCoordinator(service, clock=clock or utcnow)
    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        stopping = asyncio.Event()
        async def housekeeping():
            while not stopping.is_set():
                try:
                    await service.housekeep()
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
    app.add_middleware(Boundary)
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
                "service": "local-companion", "capabilities": service.registry.metadata()}

    @app.get("/state", response_model=State, responses=errors)
    async def state():
        return await service.state()

    @app.post("/observations", response_model=IngestionResponse, responses=errors)
    async def observations(body: ObservationBatch):
        return await service.mutate("/observations", body.model_dump())

    @app.post("/feedback", response_model=State, responses=errors)
    async def feedback(body: FeedbackRequest):
        return await service.mutate("/feedback", body.model_dump())

    @app.put("/preferences", response_model=State, responses=errors)
    async def preferences(body: PreferencesRequest):
        return await service.mutate("/preferences", body.model_dump())

    @app.post("/process", response_model=State, responses=errors)
    async def process(body: ProcessRequest):
        return await service.mutate("/process", body.model_dump())

    @app.post("/demo", response_model=State, responses=errors)
    async def demo(body: DemoRequest):
        return await service.mutate("/demo", body.model_dump())

    @app.delete("/data", response_model=State, responses=errors)
    async def delete(body: DeleteRequest):
        return await service.mutate("/data", body.model_dump())

    return app
