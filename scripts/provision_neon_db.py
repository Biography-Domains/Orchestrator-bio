#!/usr/bin/env python3
"""
Provision a Neon Postgres project/database for Orchestrator-bio.

Inputs:
- GCP Secret Manager secret: neon_api_key

Outputs:
- Writes (best-effort) GCP secrets:
  - orchestrator_bio_database_url
  - hyperlink_bio_database_url

Notes:
- This provisions a single shared DB (recommended). Schema separation can be used later.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any, Dict, Optional

import requests


"""
Neon API base.

In many environments `api.neon.tech` does not resolve, while `console.neon.tech` does.
Neon serves the REST API under the console host as well.
"""
NEON_API_BASE = os.getenv("NEON_API_BASE", "https://console.neon.tech/api/v2").rstrip("/")
DEFAULT_GCLOUD = "/home/skynet/google-cloud-sdk/bin/gcloud"


def _gcp_secret(project: str, name: str) -> str:
    out = subprocess.check_output(
        [DEFAULT_GCLOUD, "secrets", "versions", "access", "latest", f"--secret={name}", f"--project={project}"],
        text=True,
    )
    return out.strip()


def _ensure_secret(project: str, name: str) -> None:
    # Create if missing; ignore if exists.
    subprocess.run(
        [DEFAULT_GCLOUD, "secrets", "create", name, f"--project={project}", "--replication-policy=automatic"],
        text=True,
        capture_output=True,
    )


def _write_secret_version(project: str, name: str, value: str) -> None:
    _ensure_secret(project, name)
    p = subprocess.run(
        [DEFAULT_GCLOUD, "secrets", "versions", "add", name, f"--project={project}", "--data-file=-"],
        input=value,
        text=True,
        capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"Failed writing secret {name}: {p.stderr}")


@dataclass
class NeonProvisionResult:
    project_id: str
    project_name: str
    branch_id: Optional[str]
    database_name: str
    role_name: str
    connection_uri: str


def _neon_headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _neon_post(path: str, api_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(f"{NEON_API_BASE}{path}", headers=_neon_headers(api_key), json=payload, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Neon POST {path} failed: {r.status_code} {r.text}")
    return r.json()


def _neon_get(path: str, api_key: str) -> Dict[str, Any]:
    r = requests.get(f"{NEON_API_BASE}{path}", headers=_neon_headers(api_key), timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Neon GET {path} failed: {r.status_code} {r.text}")
    return r.json()


def provision_neon_project(*, api_key: str, name: str) -> NeonProvisionResult:
    # Prefer reusing an existing project by name to avoid creating duplicates.
    existing = _neon_get("/projects", api_key)
    projects = existing.get("projects") or []
    proj = next((p for p in projects if (p.get("name") or "").strip().lower() == name.strip().lower()), None)

    created: Dict[str, Any] = {}
    if not proj:
        created = _neon_post(
            "/projects",
            api_key,
            {"project": {"name": name}},
        )
        proj = created.get("project") or {}

    project_id = (proj.get("id") or "").strip()
    if not project_id:
        raise RuntimeError(f"Neon project id missing: {proj}")

    # Determine defaults from create response when available; otherwise fall back to listing.
    databases = created.get("databases") or []
    roles = created.get("roles") or []
    branch = (created.get("branch") or {}) if isinstance(created.get("branch"), dict) else {}
    branch_id = branch.get("id")

    if not databases:
        dbs = _neon_get(f"/projects/{project_id}/databases", api_key).get("databases") or []
        databases = dbs
    if not roles:
        rs = _neon_get(f"/projects/{project_id}/roles", api_key).get("roles") or []
        roles = rs

    db_name = (databases[0].get("name") if databases else "neondb") or "neondb"
    role_name = (roles[0].get("name") if roles else "neondb_owner") or "neondb_owner"

    # Connection URI endpoint varies; try official helper endpoint first.
    # Neon docs use /projects/{id}/connection_uri with database_name + role_name.
    try:
        conn = _neon_get(
            f"/projects/{project_id}/connection_uri?database_name={db_name}&role_name={role_name}&sslmode=require",
            api_key,
        )
        uri = (conn.get("uri") or conn.get("connection_uri") or "").strip()
        if not uri:
            raise RuntimeError("empty uri")
    except Exception:
        # Fallback: attempt to pull from endpoints list (may include host)
        eps = _neon_get(f"/projects/{project_id}/endpoints", api_key)
        endpoints = eps.get("endpoints") or []
        host = ""
        if endpoints:
            host = (endpoints[0].get("host") or "").strip()
        if not host:
            raise
        # User/password are not retrievable; this fallback is best-effort only.
        raise RuntimeError("Could not retrieve Neon connection URI automatically; check Neon API response.")

    # Store async SQLAlchemy-compatible URL by default.
    if uri.startswith("postgresql://"):
        uri = uri.replace("postgresql://", "postgresql+asyncpg://", 1)
    # Convert libpq-style params to asyncpg-friendly params.
    try:
        parts = urlsplit(uri)
        q_in = list(parse_qsl(parts.query, keep_blank_values=True))
        q = [(k, v) for (k, v) in q_in if k not in {"channel_binding", "sslmode"}]
        if not any(k == "ssl" for (k, _v) in q):
            q.append(("ssl", "require"))
        else:
            q = [(k, ("require" if k == "ssl" and v.lower() == "true" else v)) for (k, v) in q]
        uri = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))
    except Exception:
        pass

    return NeonProvisionResult(
        project_id=project_id,
        project_name=proj.get("name") or name,
        branch_id=branch_id,
        database_name=db_name,
        role_name=role_name,
        connection_uri=uri,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.getenv("GCP_PROJECT_ID", "superapp-466313"))
    ap.add_argument("--neon-secret", default="neon_api_key")
    ap.add_argument("--neon-project-name", default="orchestrator-bio")
    ap.add_argument("--write-secrets", action="store_true", help="Write DB URLs to GCP Secret Manager (recommended)")
    args = ap.parse_args()

    neon_key = _gcp_secret(args.project, args.neon_secret)
    res = provision_neon_project(api_key=neon_key, name=args.neon_project_name)

    print(json.dumps(res.__dict__, indent=2))
    print("\n=== DATABASE_URL ===")
    print(res.connection_uri)

    if args.write_secrets:
        _write_secret_version(args.project, "orchestrator_bio_database_url", res.connection_uri)
        _write_secret_version(args.project, "hyperlink_bio_database_url", res.connection_uri)
        print("\nOK: wrote secrets orchestrator_bio_database_url + hyperlink_bio_database_url")
    else:
        print("\nNOTE: run with --write-secrets to persist URLs in Secret Manager.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


