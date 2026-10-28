"""API entry point. Slow generation and rendering run in a separate worker."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from book_video.api.router import create_api_router

app = FastAPI(title="Book Video Workspace", version="0.0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Idempotency-Key", "Last-Event-ID"],
)
app.include_router(create_api_router())


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
