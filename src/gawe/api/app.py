"""The HTTP layer: FastAPI routes that translate requests into ApiService calls.

There is no business logic here. Authentication happens in one dependency, errors are converted to
a uniform `{"error": {"code", "message"}}` body, and validation errors never echo the request
body (it may hold a confidential document).

Endpoints (all under /v1 except /healthz; every one needs `Authorization: Bearer <token>`):

    GET  /healthz                         liveness, no auth
    GET  /v1/tools                        registered tools and their input schemas
    POST /v1/runs                         start a run (planning continues in the background)
    GET  /v1/runs                         the tenant's runs, newest first
    GET  /v1/runs/{id}                    one run with its steps and policy outcomes
    POST /v1/runs/{id}/cancel             cancel (owner or admin)
    POST /v1/runs/{id}/resume             drive the run again, e.g. after a restart (owner/admin)
    GET  /v1/runs/{id}/audit              the run's audit events
    GET  /v1/runs/{id}/audit/verify       check the hash chain and its anchor
    GET  /v1/runs/{id}/events             server-sent events: audit events as they happen
    GET  /v1/approvals                    open approvals the caller could decide
    POST /v1/approvals/{id}/decision      approve or reject, echoing the action hash shown
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from gawe.api.auth import AuthError, Identity, TokenAuthenticator, parse_bearer
from gawe.api.errors import ApiError, BadRequestError, TooManyRequestsError, UnauthorizedError
from gawe.api.schemas import (
    ApprovalView,
    AuditEventView,
    AuditVerificationView,
    MeView,
    RunView,
    ToolView,
)
from gawe.api.service import ApiService
from gawe.api.throttle import FailureThrottle
from gawe.core.audit import AnchorPoint
from gawe.llm.planner import MAX_OBJECTIVE_CHARS

DEFAULT_MAX_BODY_BYTES = 1_500_000
_SHA256 = r"^[0-9a-f]{64}$"


class StartRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE_CHARS)
    profile: str = Field(min_length=1, max_length=64)
    document: str | None = None  # length is limited by the service and the body-size cap


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    action_hash: str = Field(pattern=_SHA256)
    comment: str | None = Field(default=None, max_length=2000)


def create_app(
    *,
    service: ApiService,
    authenticator: TokenAuthenticator,
    cors_origins: Sequence[str] = (),
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    poll_interval: float = 1.0,
    heartbeat_interval: float = 15.0,
    max_stream_seconds: float = 3600.0,
    throttle: FailureThrottle | None = None,
) -> FastAPI:
    failures = throttle if throttle is not None else FailureThrottle()
    app = FastAPI(title="GAWE", version="0.1.0")

    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),  # explicit origins only, never "*"
            allow_credentials=False,  # bearer tokens travel in a header, not cookies
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
        )

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > max_body_bytes):
            return _error(413, "payload_too_large", "request body is too large")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        headers: dict[str, str] | None = None
        if exc.status == 401:
            headers = {"WWW-Authenticate": "Bearer"}
        elif isinstance(exc, TooManyRequestsError):
            headers = {"Retry-After": str(exc.retry_after)}
        return _error(exc.status, exc.code, exc.message, headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Only where and why; never the submitted values.
        details = [{"loc": [str(p) for p in e["loc"]], "msg": e["msg"]} for e in exc.errors()]
        body = {
            "error": {"code": "validation_error", "message": "invalid request", "details": details}
        }
        return JSONResponse(body, status_code=422)

    def identity(request: Request, authorization: str | None = Header(default=None)) -> Identity:
        client = request.client.host if request.client else "unknown"
        wait = failures.retry_after(client)
        if wait is not None:
            raise TooManyRequestsError("too many failed attempts; try again later", wait)
        try:
            return authenticator.authenticate(parse_bearer(authorization))
        except AuthError:
            failures.record_failure(client)
            raise UnauthorizedError("invalid credentials") from None

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/me")
    def me(who: Identity = Depends(identity)) -> MeView:
        return MeView(user_id=who.user_id, tenant_id=who.tenant_id, roles=tuple(sorted(who.roles)))

    @app.get("/v1/tools")
    def tools(who: Identity = Depends(identity)) -> list[ToolView]:
        return service.list_tools(who)

    @app.post("/v1/runs", status_code=201)
    def start_run(body: StartRunBody, who: Identity = Depends(identity)) -> RunView:
        return service.start_run(
            who, objective=body.objective, profile=body.profile, document=body.document
        )

    @app.get("/v1/runs")
    def list_runs(
        limit: int = Query(default=50, ge=1, le=100), who: Identity = Depends(identity)
    ) -> list[RunView]:
        return service.list_runs(who, limit=limit)

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, who: Identity = Depends(identity)) -> RunView:
        return service.get_run(who, run_id)

    @app.post("/v1/runs/{run_id}/cancel")
    def cancel_run(run_id: str, who: Identity = Depends(identity)) -> RunView:
        return service.cancel_run(who, run_id)

    @app.post("/v1/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str, who: Identity = Depends(identity)) -> RunView:
        return service.resume_run(who, run_id)

    @app.get("/v1/runs/{run_id}/audit")
    def audit(run_id: str, who: Identity = Depends(identity)) -> list[AuditEventView]:
        return service.audit_events(who, run_id)

    @app.get("/v1/runs/{run_id}/audit/verify")
    def verify(
        run_id: str,
        anchor_seq: int | None = Query(default=None, ge=1),
        anchor_head: str | None = Query(default=None, pattern=_SHA256),
        who: Identity = Depends(identity),
    ) -> AuditVerificationView:
        if (anchor_seq is None) != (anchor_head is None):
            raise BadRequestError("anchor_seq and anchor_head must be given together")
        anchor = (
            AnchorPoint(seq=anchor_seq, head=anchor_head)
            if anchor_seq is not None and anchor_head is not None
            else None
        )
        return service.verify_audit(who, run_id, anchor=anchor)

    @app.get("/v1/approvals")
    def inbox(who: Identity = Depends(identity)) -> list[ApprovalView]:
        return service.inbox(who)

    @app.post("/v1/approvals/{approval_id}/decision")
    def decide(
        approval_id: str, body: DecisionBody, who: Identity = Depends(identity)
    ) -> ApprovalView:
        return service.decide(
            who,
            approval_id,
            decision=body.decision,
            action_hash=body.action_hash,
            comment=body.comment,
        )

    @app.get("/v1/runs/{run_id}/events")
    async def events(
        run_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None),
        who: Identity = Depends(identity),
    ) -> StreamingResponse:
        start = after
        if last_event_id is not None and last_event_id.isdigit():
            start = max(start, int(last_event_id))
        # The first page is fetched before streaming starts so a bad id or tenant gets a 404.
        first = await run_in_threadpool(service.events_after, who, run_id, after_seq=start)

        async def stream() -> AsyncIterator[str]:
            page, seq = first, start
            deadline = time.monotonic() + max_stream_seconds
            quiet_since = time.monotonic()
            while True:
                for event in page.events:
                    seq = event.seq
                    quiet_since = time.monotonic()
                    yield f"id: {event.seq}\nevent: audit\ndata: {event.model_dump_json()}\n\n"
                if page.finished:
                    yield f"event: end\ndata: {json.dumps({'status': page.run_status})}\n\n"
                    return
                if await request.is_disconnected() or time.monotonic() > deadline:
                    return
                if time.monotonic() - quiet_since >= heartbeat_interval:
                    quiet_since = time.monotonic()
                    yield ": keepalive\n\n"
                await asyncio.sleep(poll_interval)
                try:
                    page = await run_in_threadpool(service.events_after, who, run_id, after_seq=seq)
                except ApiError:
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _error(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )
