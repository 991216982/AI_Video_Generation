"""Contract-compatible /api/v1 routes backed by the domain service."""

from __future__ import annotations

import json
from typing import Callable
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.routing import APIRoute

from book_video.api.schemas import (
    BookCreate,
    BookEdition,
    BookPage,
    BookFactRevision,
    BookFactRevisionRequest,
    AudioAsset,
    CancelRequest,
    CreativeProject,
    CreativeProjectCreate,
    CreativeProjectPage,
    CreativeWorkspace,
    Evidence,
    EvidenceCreate,
    GenerationResult,
    ModelCostRecord,
    PromotionCandidate,
    PromotionCandidateCreate,
    PromotionEligibilityUpdate,
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
        "/books",
        operation_id="listBooks",
        response_model=BookPage,
    )
    def list_books(
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1),
        q: str | None = Query(default=None, max_length=100),
    ) -> dict:
        return domain.list_books(limit=limit, cursor=cursor, q=q)

    @router.get(
        "/books/{book_id}",
        operation_id="getBook",
        response_model=BookEdition,
        response_model_exclude_none=True,
    )
    def get_book(book_id: UUID) -> dict:
        return domain.get_book(book_id)

    @router.patch("/books/{book_id}/facts/{fact_key}", operation_id="reviseBookFact",
                  response_model=BookFactRevision)
    def revise_book_fact(book_id: UUID, fact_key: str, data: BookFactRevisionRequest) -> dict:
        return domain.revise_book_fact(book_id, fact_key, data)

    @router.get("/books/{book_id}/facts/{fact_key}/revisions",
                operation_id="listBookFactRevisions", response_model=list[BookFactRevision])
    def list_book_fact_revisions(book_id: UUID, fact_key: str) -> list[dict]:
        return domain.list_book_fact_revisions(book_id, fact_key)

    @router.post(
        "/promotion-candidates",
        operation_id="createPromotionCandidate",
        response_model=PromotionCandidate,
        status_code=201,
    )
    def create_promotion_candidate(data: PromotionCandidateCreate) -> dict:
        return domain.create_promotion_candidate(data)

    @router.get(
        "/promotion-candidates/{candidate_id}",
        operation_id="getPromotionCandidate",
        response_model=PromotionCandidate,
    )
    def get_promotion_candidate(candidate_id: UUID) -> dict:
        return domain.get_promotion_candidate(candidate_id)

    @router.post("/audio-assets", operation_id="uploadAudioAsset", response_model=AudioAsset,
                 status_code=201)
    async def upload_audio_asset(
        file: UploadFile = File(), sha256: str = Form(), origin: str = Form(),
        rights_attested: bool = Form(), rights_proof_note: str = Form(),
        permitted_uses: str = Form(), expires_at: str | None = Form(default=None),
    ) -> dict:
        content = await file.read(10 * 1024 * 1024 + 1)
        return domain.upload_audio_asset(
            content, sha256=sha256, origin=origin, rights_attested=rights_attested,
            rights_proof_note=rights_proof_note, permitted_uses=permitted_uses,
            expires_at=expires_at, declared_mime_type=file.content_type,
        )

    @router.get("/audio-assets", operation_id="listAudioAssets", response_model=list[AudioAsset])
    def list_audio_assets() -> list[dict]:
        return domain.list_audio_assets()

    @router.get("/audio-assets/{audio_asset_id}", operation_id="getAudioAsset",
                response_model=AudioAsset)
    def get_audio_asset(audio_asset_id: UUID) -> dict:
        return domain.get_audio_asset(audio_asset_id)

    @router.patch(
        "/promotion-candidates/{candidate_id}/eligibility",
        operation_id="updatePromotionEligibility",
        response_model=PromotionCandidate,
    )
    def update_promotion_eligibility(
        candidate_id: UUID, data: PromotionEligibilityUpdate
    ) -> dict:
        return domain.update_promotion_eligibility(candidate_id, data)

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
        "/creative-projects",
        operation_id="listCreativeProjects",
        response_model=CreativeProjectPage,
    )
    def list_creative_projects(
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1),
        book_id: UUID | None = None,
    ) -> dict:
        return domain.list_creative_projects(
            limit=limit, cursor=cursor, book_id=book_id
        )

    @router.get(
        "/creative-projects/{project_id}",
        operation_id="getCreativeProject",
        response_model=CreativeProject,
        response_model_exclude_none=True,
    )
    def get_creative_project(project_id: UUID) -> dict:
        return domain.get_creative_project(project_id)

    @router.get(
        "/creative-projects/{project_id}/workspace",
        operation_id="getCreativeWorkspace",
        response_model=CreativeWorkspace,
    )
    def get_creative_workspace(project_id: UUID, request: Request) -> dict:
        workspace = domain.get_creative_workspace(project_id)
        package = workspace.get("publication_package")
        if package is not None:
            package["video_download_url"] = str(
                request.url_for("downloadRender", render_id=package["render_id"])
            )
        return workspace

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
    )
    def get_run(run_id: UUID) -> dict:
        return domain.get_run(run_id)

    @router.get("/runs/{run_id}/generation-results", operation_id="listGenerationResults",
                response_model=list[GenerationResult])
    def list_generation_results(run_id: UUID) -> list[dict]:
        return domain.list_generation_results(run_id)

    @router.get("/runs/{run_id}/costs", operation_id="listRunCosts",
                response_model=list[ModelCostRecord])
    def list_run_costs(run_id: UUID) -> list[dict]:
        return domain.list_run_costs(run_id)

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
    )
    def review_run(run_id: UUID, data: ReviewRequest) -> dict:
        return domain.review_run(run_id, data)

    @router.post(
        "/runs/{run_id}/cancel",
        operation_id="cancelRun",
        response_model=Run,
    )
    def cancel_run(
        run_id: UUID,
        data: CancelRequest,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ) -> dict:
        return domain.cancel_run(run_id, data, idempotency_key)

    @router.post(
        "/runs/{run_id}/retry",
        operation_id="retryRun",
        response_model=Run,
        status_code=202,
    )
    def retry_run(
        run_id: UUID,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ) -> dict:
        return domain.retry_run(run_id, idempotency_key)

    @router.post(
        "/renders",
        operation_id="startRender",
        response_model=RenderJob,
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
    )
    def get_render(render_id: UUID) -> dict:
        return domain.get_render(render_id)

    @router.post(
        "/renders/{render_id}/cancel",
        operation_id="cancelRender",
        response_model=RenderJob,
    )
    def cancel_render(
        render_id: UUID,
        data: CancelRequest,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ) -> dict:
        return domain.cancel_render(render_id, data, idempotency_key)

    @router.post(
        "/renders/{render_id}/retry",
        operation_id="retryRender",
        response_model=RenderJob,
        status_code=202,
    )
    def retry_render(
        render_id: UUID,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ) -> dict:
        return domain.retry_render(render_id, idempotency_key)

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
