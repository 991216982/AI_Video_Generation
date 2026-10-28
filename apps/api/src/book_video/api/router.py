"""Contract-compatible /api/v1 routes backed by the domain service."""

from __future__ import annotations

import json
from typing import Callable
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.routing import APIRoute

from book_video.api.schemas import (
    BookCreate,
    BookEdition,
    CreativeProject,
    CreativeProjectCreate,
    Evidence,
    EvidenceCreate,
    PromotionCandidate,
    PromotionCandidateCreate,
    PublicationPackage,
    RenderCreate,
    RenderJob,
    ReviewRequest,
    Run,
    RunCreate,
    ScriptVersion,
    ScriptVersionCreate,
)
from book_video.domain.errors import DomainError
from book_video.domain.service import DomainService, SubmissionHook


def _error_response(request: Request, error: DomainError) -> JSONResponse:
    body = {
        "code": error.code,
        "message": error.message,
        "request_id": request.headers.get("x-request-id") or str(uuid4()),
    }
    if error.details is not None:
        body["details"] = error.details
    return JSONResponse(status_code=error.status_code, content=body)


class ContractRoute(APIRoute):
    """Keep FastAPI validation and domain errors in the shared error envelope."""

    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def contract_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except DomainError as error:
                return _error_response(request, error)
            except RequestValidationError as error:
                details = {
                    "errors": [
                        {
                            "location": list(item.get("loc", ())),
                            "message": item.get("msg", "Invalid request"),
                            "type": item.get("type", "value_error"),
                        }
                        for item in error.errors()
                    ]
                }
                return _error_response(
                    request,
                    DomainError(422, "validation_error", "Request validation failed", details=details),
                )

        return contract_handler


def create_api_router(
    service: DomainService | None = None,
    *,
    run_submitter: SubmissionHook | None = None,
    render_submitter: SubmissionHook | None = None,
) -> APIRouter:
    """Build the frozen `/api/v1` API and inject worker submission hooks.

    Submission hooks receive a persisted `Run`/`RenderJob` after the request commits.
    They should only enqueue work; generation and rendering stay outside HTTP handling.
    """

    domain = service or DomainService()
    router = APIRouter(prefix="/api/v1", route_class=ContractRoute)

    @router.post(
        "/books",
        operation_id="createBook",
        response_model=BookEdition,
        response_model_exclude_none=True,
        status_code=201,
    )
    def create_book(data: BookCreate) -> dict:
        return domain.create_book(data)

    @router.get(
        "/books/{book_id}",
        operation_id="getBook",
        response_model=BookEdition,
        response_model_exclude_none=True,
    )
    def get_book(book_id: UUID) -> dict:
        return domain.get_book(book_id)

    @router.post(
        "/promotion-candidates",
        operation_id="createPromotionCandidate",
        response_model=PromotionCandidate,
        response_model_exclude_none=True,
        status_code=201,
    )
    def create_promotion_candidate(data: PromotionCandidateCreate) -> dict:
        return domain.create_promotion_candidate(data)

    @router.get(
        "/promotion-candidates/{candidate_id}",
        operation_id="getPromotionCandidate",
        response_model=PromotionCandidate,
        response_model_exclude_none=True,
    )
    def get_promotion_candidate(candidate_id: UUID) -> dict:
        return domain.get_promotion_candidate(candidate_id)

    @router.post(
        "/evidence",
        operation_id="createEvidence",
        response_model=Evidence,
        response_model_exclude_none=True,
        status_code=201,
    )
    def create_evidence(data: EvidenceCreate) -> dict:
        return domain.create_evidence(data)

    @router.post(
        "/creative-projects",
        operation_id="createCreativeProject",
        response_model=CreativeProject,
        response_model_exclude_none=True,
        status_code=201,
    )
    def create_creative_project(data: CreativeProjectCreate) -> dict:
        return domain.create_creative_project(data)

    @router.get(
        "/creative-projects/{project_id}",
        operation_id="getCreativeProject",
        response_model=CreativeProject,
        response_model_exclude_none=True,
    )
    def get_creative_project(project_id: UUID) -> dict:
        return domain.get_creative_project(project_id)

    @router.get(
        "/creative-projects/{project_id}/scripts",
        operation_id="listScriptVersions",
        response_model=list[ScriptVersion],
        response_model_exclude_none=True,
    )
    def list_script_versions(project_id: UUID) -> list[dict]:
        return domain.list_script_versions(project_id)

    @router.post(
        "/creative-projects/{project_id}/scripts",
        operation_id="saveScriptVersion",
        response_model=ScriptVersion,
        response_model_exclude_none=True,
        status_code=201,
    )
    def save_script_version(project_id: UUID, data: ScriptVersionCreate) -> dict:
        return domain.save_script_version(project_id, data)

    @router.post(
        "/creative-projects/{project_id}/runs",
        operation_id="startRun",
        response_model=Run,
        response_model_exclude_none=True,
        status_code=202,
    )
    def start_run(
        project_id: UUID,
        data: RunCreate,
        background_tasks: BackgroundTasks,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ) -> dict:
        def enqueue(run: dict) -> None:
            if run_submitter is not None:
                background_tasks.add_task(run_submitter, run)

        return domain.create_run(
            project_id,
            data,
            idempotency_key,
            submitter=enqueue if run_submitter is not None else None,
        )

    @router.get(
        "/runs/{run_id}",
        operation_id="getRun",
        response_model=Run,
        response_model_exclude_none=True,
    )
    def get_run(run_id: UUID) -> dict:
        return domain.get_run(run_id)

    @router.get(
        "/runs/{run_id}/events",
        operation_id="streamRunEvents",
        response_class=Response,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
    )
    def stream_run_events(
        run_id: UUID,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> Response:
        after_sequence = 0
        if last_event_id not in (None, ""):
            try:
                after_sequence = int(last_event_id)
            except ValueError as exc:
                raise DomainError(
                    422, "invalid_sequence", "Last-Event-ID must be an integer"
                ) from exc
        events = domain.list_run_events(run_id, after_sequence=after_sequence)
        body = "".join(
            f"id: {event['sequence']}\nevent: {event['type']}\ndata: "
            f"{json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
            for event in events
        )
        return Response(content=body, media_type="text/event-stream")

    @router.post(
        "/runs/{run_id}/review",
        operation_id="reviewRun",
        response_model=Run,
        response_model_exclude_none=True,
    )
    def review_run(run_id: UUID, data: ReviewRequest) -> dict:
        return domain.review_run(run_id, data)

    @router.post(
        "/renders",
        operation_id="startRender",
        response_model=RenderJob,
        response_model_exclude_none=True,
        status_code=202,
    )
    def start_render(
        data: RenderCreate,
        background_tasks: BackgroundTasks,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ) -> dict:
        def enqueue(render: dict) -> None:
            if render_submitter is not None:
                background_tasks.add_task(render_submitter, render)

        return domain.create_render(
            data,
            idempotency_key,
            submitter=enqueue if render_submitter is not None else None,
        )

    @router.get(
        "/renders/{render_id}",
        operation_id="getRender",
        response_model=RenderJob,
        response_model_exclude_none=True,
    )
    def get_render(render_id: UUID) -> dict:
        return domain.get_render(render_id)

    @router.get(
        "/renders/{render_id}/download",
        operation_id="downloadRender",
        name="downloadRender",
        response_class=FileResponse,
        responses={
            200: {
                "content": {"video/mp4": {"schema": {"type": "string", "format": "binary"}}}
            },
            404: {"description": "Render not found or not owned by this user"},
            409: {"description": "Render is incomplete or output is unavailable"},
        },
    )
    def download_render(render_id: UUID) -> FileResponse:
        path = domain.get_render_download_path(render_id)
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=path.name,
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get(
        "/creative-projects/{project_id}/publication-package",
        operation_id="getPublicationPackage",
        response_model=PublicationPackage,
        response_model_exclude_none=True,
    )
    def get_publication_package(project_id: UUID, request: Request) -> dict:
        package, render_id = domain.get_publication_package_record(project_id)
        package["video_download_url"] = str(
            request.url_for("downloadRender", render_id=render_id)
        )
        return package

    return router
