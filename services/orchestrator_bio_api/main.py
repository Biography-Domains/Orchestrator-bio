from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(
    title="orchestrator-bio",
    version="0.1.0",
    description="Bio-specific orchestrator API (jobs, runs, site registry).",
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "orchestrator-bio"}


