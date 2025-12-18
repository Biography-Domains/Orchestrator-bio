from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.hyperlink_bio_api.db import create_engine, create_sessionmaker, get_database_url
from services.hyperlink_bio_api.models import Base, Comment, Hostname, Site, Vote


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = create_engine()
    session_maker = create_sessionmaker(engine)
    app.state.engine = engine
    app.state.session_maker = session_maker
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(
    title="hyperlink-bio",
    version="0.1.0",
    description="Bio interactions API (votes/comments). Postgres-backed in Orchestrator-bio.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "hyperlink-bio", "db": "configured" if get_database_url() else "missing"}


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


def _norm_host(hostname: str) -> str:
    return (hostname or "").strip().lower()


def _norm_site_key(site_key: str) -> str:
    return (site_key or "").strip().lower()


async def _resolve_site_id(
    *,
    session: AsyncSession,
    site_key: Optional[str],
    hostname: Optional[str],
) -> int:
    if site_key:
        res = await session.execute(select(Site).where(Site.site_key == _norm_site_key(site_key)))
        s = res.scalar_one_or_none()
        if not s:
            raise HTTPException(status_code=404, detail="site_key not found")
        return int(s.id)

    if hostname:
        hn = _norm_host(hostname)
        res = await session.execute(select(Hostname).where(Hostname.hostname == hn))
        h = res.scalar_one_or_none()
        if not h:
            raise HTTPException(status_code=404, detail="hostname not registered")
        return int(h.site_id)

    raise HTTPException(status_code=400, detail="Provide site_key or hostname")


class SiteRegister(BaseModel):
    site_key: str = Field(..., description="Canonical bio site key, e.g. bio-bob-dylan")
    display_name: Optional[str] = None
    primary_domain: Optional[str] = None
    hostnames: list[str] = Field(default_factory=list, description="Hostnames mapping to this site (doorway + hub)")


@app.post("/sites/register")
async def register_site(payload: SiteRegister, session: AsyncSession = Depends(get_session)):
    sk = _norm_site_key(payload.site_key)
    if not sk:
        raise HTTPException(status_code=400, detail="site_key required")

    res = await session.execute(select(Site).where(Site.site_key == sk))
    site = res.scalar_one_or_none()
    if not site:
        site = Site(site_key=sk, display_name=payload.display_name, primary_domain=payload.primary_domain)
        session.add(site)
        await session.flush()
    else:
        if payload.display_name is not None:
            site.display_name = payload.display_name
        if payload.primary_domain is not None:
            site.primary_domain = payload.primary_domain

    for hn in payload.hostnames:
        hn2 = _norm_host(hn)
        if not hn2:
            continue
        session.add(Hostname(hostname=hn2, site_id=site.id))
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            # ignore duplicates
            async with session.begin():
                pass

    return {"ok": True, "site_id": site.id, "site_key": site.site_key}


class VoteCreate(BaseModel):
    site_key: Optional[str] = None
    hostname: Optional[str] = None
    entity_type: str
    entity_key: str
    choice: str
    voter_id: Optional[str] = None


@app.post("/votes")
async def create_vote(payload: VoteCreate, session: AsyncSession = Depends(get_session)):
    site_id = await _resolve_site_id(session=session, site_key=payload.site_key, hostname=payload.hostname)
    v = Vote(
        site_id=site_id,
        entity_type=payload.entity_type,
        entity_key=payload.entity_key,
        choice=payload.choice,
        voter_id=payload.voter_id,
        created_at=datetime.utcnow(),
    )
    session.add(v)
    try:
        await session.flush()
    except IntegrityError:
        # idempotent duplicates
        await session.rollback()
        return {"ok": True, "deduped": True}
    return {"ok": True, "id": v.id}


@app.get("/votes/tally")
async def vote_tally(
    site_key: Optional[str] = None,
    hostname: Optional[str] = None,
    entity_type: str = Query(...),
    entity_key: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    site_id = await _resolve_site_id(session=session, site_key=site_key, hostname=hostname)
    stmt = (
        select(Vote.choice, func.count(Vote.id))
        .where(Vote.site_id == site_id, Vote.entity_type == entity_type, Vote.entity_key == entity_key)
        .group_by(Vote.choice)
    )
    res = await session.execute(stmt)
    totals = {choice: int(cnt) for choice, cnt in res.all()}
    return {"site_id": site_id, "entity_type": entity_type, "entity_key": entity_key, "totals": totals}


class CommentCreate(BaseModel):
    site_key: Optional[str] = None
    hostname: Optional[str] = None
    entity_type: str
    entity_key: str
    author: Optional[str] = None
    body: str


@app.post("/comments")
async def create_comment(payload: CommentCreate, session: AsyncSession = Depends(get_session)):
    site_id = await _resolve_site_id(session=session, site_key=payload.site_key, hostname=payload.hostname)
    if not payload.body.strip():
        raise HTTPException(status_code=400, detail="body required")
    c = Comment(
        site_id=site_id,
        entity_type=payload.entity_type,
        entity_key=payload.entity_key,
        author=(payload.author or "").strip() or None,
        body=payload.body.strip(),
        created_at=datetime.utcnow(),
    )
    session.add(c)
    await session.flush()
    return {"id": c.id, "site_id": site_id, "entity_type": c.entity_type, "entity_key": c.entity_key, "author": c.author, "body": c.body, "created_at": c.created_at}


@app.get("/comments")
async def list_comments(
    site_key: Optional[str] = None,
    hostname: Optional[str] = None,
    entity_type: str = Query(...),
    entity_key: str = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    site_id = await _resolve_site_id(session=session, site_key=site_key, hostname=hostname)
    stmt = (
        select(Comment)
        .where(Comment.site_id == site_id, Comment.entity_type == entity_type, Comment.entity_key == entity_key)
        .order_by(Comment.created_at.asc())
        .offset(offset)
        .limit(limit)
    )
    res = await session.execute(stmt)
    out = []
    for c in res.scalars().all():
        out.append(
            {
                "id": c.id,
                "site_id": site_id,
                "entity_type": c.entity_type,
                "entity_key": c.entity_key,
                "author": c.author,
                "body": c.body,
                "created_at": c.created_at,
            }
        )
    return out


