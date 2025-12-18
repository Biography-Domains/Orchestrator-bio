from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def get_database_url() -> str:
    """
    Shared Postgres URL for both orchestrator-bio and hyperlink-bio.

    Priority:
    - HYPERLINK_BIO_DATABASE_URL
    - ORCHESTRATOR_BIO_DATABASE_URL
    - DATABASE_URL
    """
    return (
        os.getenv("HYPERLINK_BIO_DATABASE_URL")
        or os.getenv("ORCHESTRATOR_BIO_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()


def create_engine(database_url: Optional[str] = None) -> AsyncEngine:
    url = (database_url or get_database_url()).strip()
    if not url:
        raise RuntimeError("Missing database URL (set HYPERLINK_BIO_DATABASE_URL or ORCHESTRATOR_BIO_DATABASE_URL).")
    # Neon connection URIs are often returned as "postgresql://...".
    # SQLAlchemy async requires an async driver (asyncpg).
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    # Neon/libpq-style params (sslmode, channel_binding) are not accepted by asyncpg.
    # Convert to asyncpg-friendly params.
    try:
        parts = urlsplit(url)
        q_in = list(parse_qsl(parts.query, keep_blank_values=True))
        q = [(k, v) for (k, v) in q_in if k not in {"channel_binding", "sslmode"}]
        # Ensure SSL is enabled for Neon.
        if not any(k == "ssl" for (k, _v) in q):
            q.append(("ssl", "require"))
        else:
            q = [(k, ("require" if k == "ssl" and v.lower() == "true" else v)) for (k, v) in q]
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))
    except Exception:
        pass
    return create_async_engine(url, echo=False, future=True)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


