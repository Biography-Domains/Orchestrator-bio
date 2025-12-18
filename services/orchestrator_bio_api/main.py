from __future__ import annotations

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")  # queued|running|success|failed
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


def _db_url() -> str:
    url = (
        os.getenv("ORCHESTRATOR_BIO_DATABASE_URL")
        or os.getenv("HYPERLINK_BIO_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    # keep it simple: strip problematic libpq params for asyncpg
    url = url.replace("sslmode=require", "").replace("channel_binding=require", "")
    return url


def _create_engine() -> AsyncEngine:
    url = _db_url()
    if not url:
        raise RuntimeError("Missing ORCHESTRATOR_BIO_DATABASE_URL (or DATABASE_URL)")
    return create_async_engine(url, echo=False, future=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = _create_engine()
    session_maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    app.state.engine = engine
    app.state.session_maker = session_maker
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(
    title="orchestrator-bio",
    version="0.1.0",
    description="Bio-specific orchestrator API (jobs, runs, scheduling).",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "orchestrator-bio"}


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    return app.state.session_maker


async def get_session(
    session_maker: async_sessionmaker[AsyncSession] = Depends(get_session_maker),
) -> AsyncSession:
    async with session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


class EnqueueJob(BaseModel):
    job_type: str = Field(..., description="e.g. generate_site, refresh_media, deploy_domain")
    payload: dict[str, Any] = Field(default_factory=dict)


@app.post("/jobs")
async def enqueue_job(req: EnqueueJob, session: AsyncSession = Depends(get_session)):
    j = Job(job_type=req.job_type.strip(), status="queued", payload_json=json.dumps(req.payload))
    session.add(j)
    await session.flush()
    return {"ok": True, "job_id": j.id}


@app.get("/jobs/{job_id}")
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)):
    j = await session.get(Job, job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "id": j.id,
        "job_type": j.job_type,
        "status": j.status,
        "payload": json.loads(j.payload_json or "{}"),
        "created_at": j.created_at,
        "started_at": j.started_at,
        "finished_at": j.finished_at,
        "last_error": j.last_error,
    }


@app.get("/jobs")
async def list_jobs(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Job).order_by(Job.id.desc()).limit(limit)
    if status:
        stmt = select(Job).where(Job.status == status).order_by(Job.id.desc()).limit(limit)
    res = await session.execute(stmt)
    jobs = []
    for j in res.scalars().all():
        jobs.append({"id": j.id, "job_type": j.job_type, "status": j.status, "created_at": j.created_at})
    return jobs


# ---------------- Worker / Scheduler (MVP) ----------------

async def _run_one_job(session_maker: async_sessionmaker[AsyncSession]) -> bool:
    async with session_maker() as session:
        res = await session.execute(select(Job).where(Job.status == "queued").order_by(Job.id.asc()).limit(1))
        j = res.scalar_one_or_none()
        if not j:
            return False
        j.status = "running"
        j.started_at = datetime.utcnow()
        await session.commit()

    # Simulated work for MVP (next step will call pipelines)
    await asyncio.sleep(0.2)

    async with session_maker() as session2:
        j2 = await session2.get(Job, j.id)
        if not j2:
            return True
        j2.status = "success"
        j2.finished_at = datetime.utcnow()
        await session2.commit()
    return True


@app.post("/worker/tick")
async def worker_tick(session_maker: async_sessionmaker[AsyncSession] = Depends(get_session_maker)):
    did = await _run_one_job(session_maker)
    return {"ok": True, "processed": did}


@app.post("/scheduler/enqueue-nightly")
async def enqueue_nightly(session: AsyncSession = Depends(get_session)):
    # placeholder: enqueue a nightly refresh job; later this will enumerate sites table.
    j = Job(job_type="nightly_refresh", status="queued", payload_json=json.dumps({"scope": "all"}))
    session.add(j)
    await session.flush()
    return {"ok": True, "job_id": j.id}


