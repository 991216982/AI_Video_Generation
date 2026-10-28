"""Stage 0 application entry point; feature routers are wired by integration."""

from fastapi import FastAPI

app = FastAPI(title="Book Video Workspace", version="0.0.1")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
