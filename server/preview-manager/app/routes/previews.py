"""Preview CRUD and action endpoints — multi-tenant org-scoped, cloud VM version."""

import json
import logging
import re
import time
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from typing import Optional
from pydantic import BaseModel

from app.docker_compose import container_prefix
from app.models import PreviewInfo
from app.state import PreviewStateManager
from app.database import (
    get_all_previews, get_preview, delete_preview_from_db,
    list_deployments as db_list_deployments,
    get_deployment as db_get_deployment,
    update_preview_vm,
    get_project_by_slug,
    compute_url_hash,
    update_last_accessed,
    list_user_organizations,
    get_running_deployment,
    finish_deployment,
)
from app.auth.dependencies import require_project_role, get_current_user
from config.settings import settings
from app.auth.models import OrgRole, UserWithContext
from app.auth import database as auth_db
from app.cloud import cloud_manager
from app.remote import RemoteExecutor
from app.deployment import VM_PREVIEW_DIR

logger = logging.getLogger(__name__)

# Org-scoped router for /api/orgs/{org}/projects/{project}/previews/...
router = APIRouter(prefix="/api/orgs/{org}/projects/{project}")

# Separate router for cross-org preview listing (used by websocket, superadmin)
global_router = APIRouter()


def _sanitize_branch_name(branch: str) -> str:
    """Sanitize a branch name for use in preview_name."""
    sanitized = branch.replace("/", "--")
    sanitized = re.sub(r"[^a-zA-Z0-9\-]", "", sanitized)
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    return sanitized


def _resolve_drush_uri(org_slug: str, project_slug: str, preview_name: str, url_hash: str) -> str:
    """Resolve drush --uri from druploy.yml config.

    If drush_uri is a simple name (e.g. "admin"), it's treated as a domain alias
    and expanded to https://{alias}--{url_hash}.{settings.preview_domain}.
    If not set or false, returns the default preview URL.
    """
    from app.docker_compose import parse_preview_yml
    from app.state import PreviewStateManager

    preview_path = PreviewStateManager.get_preview_path(org_slug, project_slug, preview_name)
    try:
        config = parse_preview_yml(preview_path)
        raw = config.get("drush_uri")
    except Exception:
        raw = None

    if not raw:
        return f"https://{url_hash}.{settings.preview_domain}"

    # If it looks like a full URL, use as-is
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw

    # Otherwise treat as a domain alias prefix
    return f"https://{raw}--{url_hash}.{settings.preview_domain}"


async def _resolve_project(user: UserWithContext, project_slug: str) -> dict:
    """Resolve a project from org context. Raises 404 if not found."""
    project = await get_project_by_slug(user.org.id, project_slug)
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{project_slug}' not found in organization '{user.org.slug}'",
        )
    return project


def _build_preview_info(state: dict) -> PreviewInfo:
    """Build a PreviewInfo response from a DB row dict."""
    last_deployment = None
    if state.get("last_deployment_status"):
        last_deployment = {
            "commit_sha": state["commit_sha"],
            "status": state["last_deployment_status"],
            "completed_at": state.get("last_deployment_completed_at"),
        }
        if state.get("last_deployment_error"):
            last_deployment["error"] = state["last_deployment_error"]
        if state.get("last_deployment_duration") is not None:
            last_deployment["duration_seconds"] = state["last_deployment_duration"]

    env_vars = state.get("env_vars", "{}")
    if isinstance(env_vars, str):
        try:
            env_vars = json.loads(env_vars) if env_vars else {}
        except (json.JSONDecodeError, TypeError):
            env_vars = {}

    org_slug = state.get("org_slug", "")
    project_slug = state.get("project_slug", state.get("project", ""))
    url_hash = state.get("url_hash", "")

    # Stack info from DB (saved by VM agent during deploy)
    stack: dict[str, str] = {}
    stack_info_raw = state.get("stack_info")
    if stack_info_raw:
        try:
            stack = json.loads(stack_info_raw) if isinstance(stack_info_raw, str) else stack_info_raw
        except (json.JSONDecodeError, TypeError):
            pass

    # Exposed services from expose_config in DB
    exposed_services: dict[str, str] = {}
    expose_config_raw = state.get("expose_config")
    if expose_config_raw and url_hash:
        try:
            expose_config = json.loads(expose_config_raw) if isinstance(expose_config_raw, str) else expose_config_raw
            preview_domain = f"{url_hash}.{settings.preview_domain}"
            for svc_name in expose_config:
                exposed_services[svc_name] = f"https://{svc_name}--{preview_domain}"
        except (json.JSONDecodeError, TypeError):
            pass

    # Domain aliases from DB (saved by VM agent during deploy)
    domain_aliases: dict[str, str] = {}
    aliases_raw = state.get("domain_aliases")
    if aliases_raw and url_hash:
        try:
            aliases_list = json.loads(aliases_raw) if isinstance(aliases_raw, str) else aliases_raw
            for prefix in aliases_list:
                domain_aliases[prefix] = f"https://{prefix}--{url_hash}.{settings.preview_domain}"
        except (json.JSONDecodeError, TypeError):
            pass

    return PreviewInfo(
        preview_name=state["preview_name"],
        project_slug=project_slug,
        org_slug=org_slug,
        url_hash=url_hash,
        mr_id=state.get("mr_id"),
        mr_title=state.get("mr_title"),
        target_branch=state.get("target_branch"),
        status=state["status"],
        url=state["url"],
        path=state["path"],
        branch=state["branch"],
        commit_sha=state["commit_sha"],
        created_at=state["created_at"],
        last_deployed_at=state.get("last_deployed_at"),
        last_deployment=last_deployment,
        auto_update=bool(state.get("auto_update", 1)),
        pinned=bool(state.get("pinned", 0)),
        env_vars=env_vars,
        vm_ip=state.get("vm_ip"),
        post_deploy_status=state.get("post_deploy_status"),
        gitlab_project_path=state.get("gitlab_project_path"),
        domain_aliases=domain_aliases,
        exposed_services=exposed_services,
        stack=stack,
    )


# ---------------------------------------------------------------------------
# Branch preview creation
# ---------------------------------------------------------------------------


class CreateBranchPreviewRequest(BaseModel):
    branch: str


class CreateMrPreviewRequest(BaseModel):
    mr_iid: int


@router.post("/previews/mr")
async def create_mr_preview(
    request: Request,
    project: str,
    body: CreateMrPreviewRequest,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Create a preview from a merge request."""
    import httpx
    from app.routes.gitlab import _get_org_gitlab_token

    proj = await _resolve_project(user, project)
    project_id = proj["id"]
    project_slug = proj["slug"]
    org_slug = user.org.slug

    preview_name = f"mr-{body.mr_iid}"

    existing = await get_preview(project_id, preview_name)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Preview {preview_name} already exists for project {project_slug}"
        )

    gitlab_url, token = await _get_org_gitlab_token(user.org.id)
    project_path = proj.get("gitlab_project_path")
    if not project_path:
        raise HTTPException(status_code=404, detail=f"Project '{project_slug}' has no GitLab project path configured")
    encoded_path = project_path.replace("/", "%2F")

    # Fetch MR details from GitLab
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{gitlab_url}/api/v4/projects/{encoded_path}/merge_requests/{body.mr_iid}",
                headers={"PRIVATE-TOKEN": token},
                timeout=15,
            )
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"MR !{body.mr_iid} not found")
            resp.raise_for_status()
            mr_data = resp.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching MR info: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e}")

    source_branch = mr_data["source_branch"]
    target_branch = mr_data["target_branch"]
    commit_sha = mr_data.get("sha", "")
    mr_title = mr_data.get("title", "")

    url_hash = compute_url_hash(org_slug, project_slug, preview_name)
    preview_url = f"https://{url_hash}.{settings.preview_domain}"

    await PreviewStateManager.save_state(
        project_id, preview_name,
        branch=source_branch,
        commit_sha=commit_sha,
        status="pending",
        url=preview_url,
        url_hash=url_hash,
        mr_id=body.mr_iid,
        mr_title=mr_title,
        target_branch=target_branch,
    )

    logger.info(
        f"Enqueued task_deploy_preview: {project_slug}/{preview_name} "
        f"triggered_by={user.email} mr_iid={body.mr_iid} branch={source_branch} commit={commit_sha[:8]}"
    )
    await request.app.state.arq.enqueue_job(
        "task_deploy_preview",
        user.org.id, org_slug, project_id, project_slug, project_path,
        preview_name, source_branch, commit_sha, user.email,
        body.mr_iid, False, mr_title, target_branch,
        _expires=timedelta(hours=3),
    )

    return {
        "success": True,
        "preview_name": preview_name,
        "mr_iid": body.mr_iid,
        "branch": source_branch,
        "commit_sha": commit_sha,
        "message": f"Creating preview {preview_name} from MR !{body.mr_iid}",
    }


@router.post("/previews/branch")
async def create_branch_preview(
    request: Request,
    project: str,
    body: CreateBranchPreviewRequest,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Create a preview from a branch (not tied to a MR)."""
    import httpx
    from app.routes.gitlab import _get_org_gitlab_token

    proj = await _resolve_project(user, project)
    project_id = proj["id"]
    project_slug = proj["slug"]
    org_slug = user.org.slug

    sanitized = _sanitize_branch_name(body.branch)
    if not sanitized:
        raise HTTPException(status_code=400, detail="Invalid branch name")

    preview_name = f"branch-{sanitized}"

    existing = await get_preview(project_id, preview_name)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Preview {preview_name} already exists for project {project_slug}"
        )

    gitlab_url, token = await _get_org_gitlab_token(user.org.id)
    project_path = proj.get("gitlab_project_path")
    if not project_path:
        raise HTTPException(status_code=404, detail=f"Project '{project_slug}' has no GitLab project path configured")
    encoded_path = project_path.replace("/", "%2F")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{gitlab_url}/api/v4/projects/{encoded_path}/repository/branches/{quote(body.branch, safe='')}",
                headers={"PRIVATE-TOKEN": token},
                timeout=15,
            )
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Branch '{body.branch}' not found")
            resp.raise_for_status()
            branch_data = resp.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching branch info: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e}")

    commit_sha = branch_data["commit"]["id"]
    url_hash = compute_url_hash(org_slug, project_slug, preview_name)
    preview_url = f"https://{url_hash}.{settings.preview_domain}"

    await PreviewStateManager.save_state(
        project_id, preview_name,
        branch=body.branch,
        commit_sha=commit_sha,
        status="pending",
        url=preview_url,
        url_hash=url_hash,
        auto_update=0,
    )

    logger.info(
        f"Enqueued task_deploy_preview: {project_slug}/{preview_name} "
        f"triggered_by={user.email} branch={body.branch} commit={commit_sha[:8]}"
    )
    await request.app.state.arq.enqueue_job(
        "task_deploy_preview",
        user.org.id, org_slug, project_id, project_slug, project_path,
        preview_name, body.branch, commit_sha, user.email,
        _expires=timedelta(hours=3),
    )

    return {
        "success": True,
        "preview_name": preview_name,
        "branch": body.branch,
        "commit_sha": commit_sha,
        "url_hash": url_hash,
        "message": f"Creating preview {preview_name} from branch {body.branch}",
    }


# ---------------------------------------------------------------------------
# Preview CRUD
# ---------------------------------------------------------------------------


@router.get("/previews/{preview_name}", response_model=PreviewInfo)
async def get_preview_endpoint(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.viewer)),
):
    proj = await _resolve_project(user, project)
    state = await PreviewStateManager.load_state(proj["id"], preview_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")
    # Enrich state with org/project slugs and gitlab path
    state["org_slug"] = user.org.slug
    state["project_slug"] = proj["slug"]
    state["gitlab_project_path"] = proj.get("gitlab_project_path")
    return _build_preview_info(state)


@router.get("/previews/{preview_name}/preview-config")
async def get_preview_config(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.viewer)),
):
    """Proxy to the VM agent's /config endpoint to get druploy.yml config."""
    import httpx

    proj = await _resolve_project(user, project)
    state = await PreviewStateManager.load_state(proj["id"], preview_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")

    vm_ip = state.get("vm_ip")
    if not vm_ip:
        raise HTTPException(status_code=400, detail="Preview has no VM assigned")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"http://{vm_ip}:8022/info")
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"VM agent returned HTTP {resp.status_code}: {resp.text}",
                )
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Could not connect to VM agent")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="VM agent request timed out")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error proxying to VM agent: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"VM agent error: {e}")


class InjectSSHKeyRequest(BaseModel):
    public_key: str


@router.post("/previews/{preview_name}/ssh-keys")
async def inject_ssh_key(
    project: str,
    preview_name: str,
    body: InjectSSHKeyRequest,
    user: UserWithContext = Depends(require_project_role(OrgRole.viewer)),
):
    """Proxy SSH key injection to the VM agent."""
    import httpx

    proj = await _resolve_project(user, project)
    state = await PreviewStateManager.load_state(proj["id"], preview_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")

    vm_ip = state.get("vm_ip")
    if not vm_ip:
        raise HTTPException(status_code=400, detail="Preview has no VM assigned")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"http://{vm_ip}:8022/ssh-keys",
                json={"public_key": body.public_key},
            )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"VM agent returned HTTP {resp.status_code}: {resp.text}",
                )
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Could not connect to VM agent")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="VM agent request timed out")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error proxying SSH key to VM agent: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"VM agent error: {e}")


class UpdatePreviewRequest(BaseModel):
    auto_update: Optional[bool] = None
    pinned: Optional[bool] = None
    env_vars: Optional[dict[str, str]] = None


@router.patch("/previews/{preview_name}")
async def update_preview_endpoint(
    project: str,
    preview_name: str,
    body: UpdatePreviewRequest,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    proj = await _resolve_project(user, project)
    project_id = proj["id"]

    state = await PreviewStateManager.load_state(project_id, preview_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")

    updates = {}
    if body.auto_update is not None:
        updates["auto_update"] = int(body.auto_update)
    if body.pinned is not None:
        updates["pinned"] = int(body.pinned)
        # When unpinning, reset last_accessed_at so the preview gets a fresh grace period
        if not body.pinned:
            from datetime import datetime, timezone
            updates["last_accessed_at"] = datetime.now(timezone.utc).isoformat()
    if body.env_vars is not None:
        updates["env_vars"] = json.dumps(body.env_vars)

    if updates:
        await PreviewStateManager.save_state(project_id, preview_name, **updates)
        from app.websockets import preview_list_manager
        await preview_list_manager.force_broadcast()

    updated = await PreviewStateManager.load_state(project_id, preview_name)
    updated["org_slug"] = user.org.slug
    updated["project_slug"] = proj["slug"]
    updated["gitlab_project_path"] = proj.get("gitlab_project_path")
    result = _build_preview_info(updated)

    if body.env_vars is not None:
        return {**result.model_dump(), "needs_rebuild": True}

    return result


def _get_preview_status(preview: dict) -> str:
    """Determine preview status based on VM state and active deployments."""
    db_status = preview.get("status", "unknown")

    # CI gating states take priority
    if db_status in ("waiting_for_ci",) or (db_status == "failed" and preview.get("ci_status") == "failed"):
        return db_status

    # If there's an active deployment running, the preview is building
    # But if post-deploy is running, the main deploy succeeded — preview is active
    latest_status = preview.get("latest_deployment_status")
    if latest_status and latest_status == "running":
        if preview.get("post_deploy_status") and preview.get("vm_id"):
            return "running"
        return "building"

    if preview.get("vm_id"):
        return "running"
    elif db_status in ("creating", "pending"):
        return db_status
    else:
        return db_status


async def get_preview_list_base(
    include_docker_status: bool = True,
    org_id: Optional[int] = None,
) -> dict:
    """Core logic to list all previews with cloud VM status.

    This is an internal helper, not a route endpoint.
    """
    t_total = time.monotonic()

    rows = await get_all_previews(org_id=org_id)
    t_db = time.monotonic()
    logger.info(f"[TIMING] DB query: {t_db - t_total:.3f}s ({len(rows)} previews found)")

    previews = []
    for row in rows:
        last_deployment = None
        latest_dep_id = row.get("latest_deployment_id")
        if latest_dep_id:
            latest_status = row.get("latest_deployment_status")
            if latest_status and row.get("latest_deployment_completed_at"):
                last_deployment = {
                    "id": latest_dep_id,
                    "status": latest_status,
                    "completed_at": row.get("latest_deployment_completed_at"),
                }
                if row.get("last_deployment_error"):
                    last_deployment["error"] = row["last_deployment_error"]
                if row.get("last_deployment_duration") is not None:
                    last_deployment["duration_seconds"] = row["last_deployment_duration"]
            else:
                last_deployment = {"id": latest_dep_id, "status": "running"}

        # Determine status from VM state
        status = _get_preview_status(row)

        url_hash = row.get("url_hash", "")

        previews.append({
            "name": row["preview_name"],
            "project": row.get("project_slug", ""),
            "project_slug": row.get("project_slug", ""),
            "org_slug": row.get("org_slug", ""),
            "url_hash": url_hash,
            "mr_id": row.get("mr_id"),
            "mr_title": row.get("mr_title"),
            "status": status,
            "url": row["url"],
            "branch": row["branch"],
            "commit_sha": row["commit_sha"],
            "last_deployed_at": row.get("last_deployed_at"),
            "last_deployment": last_deployment,
            "post_deploy_status": row.get("post_deploy_status"),
            "latest_post_deploy_id": row.get("latest_post_deploy_id"),
            "auto_update": bool(row.get("auto_update", 1)),
            "pinned": bool(row.get("pinned", 0)),
        })

    logger.info(f"[TIMING] get_preview_list_base TOTAL: {time.monotonic() - t_total:.3f}s")

    return {
        "previews": previews,
        "total": len(previews)
    }


async def delete_preview_internal(
    org_slug: str, project_slug: str, project_id: int, preview_name: str,
):
    """Core delete logic: destroy VM, remove from DB."""
    # Clear any in-flight deploy lock so a recreated preview won't be blocked
    from app.routes.webhooks import clear_deploy_lock
    await clear_deploy_lock(project_slug, preview_name)

    preview = await get_preview(project_id, preview_name)

    # Delete from DB first — this is the most important step and must always happen
    await PreviewStateManager.delete_state(project_id, preview_name)
    logger.info(f"DB record deleted for {org_slug}/{project_slug}/{preview_name}")

    # Destroy VM if exists
    if preview and preview.get("vm_id"):
        try:
            await cloud_manager.destroy_vm(preview["vm_id"])
            logger.info(f"Destroyed VM for {org_slug}/{project_slug}/{preview_name}")
        except Exception as e:
            logger.warning(f"Error destroying VM for {org_slug}/{project_slug}/{preview_name}: {e}")

    # Remove Caddy direct route
    try:
        from app.caddy_api import caddy_manager
        url_hash = preview.get("url_hash", "") if preview else compute_url_hash(org_slug, project_slug, preview_name)
        domain = f"{url_hash}.{settings.preview_domain}"
        await caddy_manager.remove_preview_route(domain)
    except Exception as e:
        logger.warning(f"Error removing Caddy route for {org_slug}/{project_slug}/{preview_name}: {e}")

    from app.websockets import preview_list_manager
    await preview_list_manager.force_broadcast()

    logger.info(f"Preview {org_slug}/{project_slug}/{preview_name} fully deleted")


@router.delete("/previews/{preview_name}")
async def delete_preview(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    proj = await _resolve_project(user, project)
    project_id = proj["id"]

    state = await PreviewStateManager.load_state(project_id, preview_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")

    try:
        await delete_preview_internal(user.org.slug, proj["slug"], project_id, preview_name)
        return {
            "success": True,
            "message": f"Preview {project}/{preview_name} deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting preview: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Global preview list (cross-org, for websocket / superadmin)
# ---------------------------------------------------------------------------


@global_router.get("/api/previews")
async def list_previews(
    status: bool = True,
    user: UserWithContext = Depends(get_current_user),
):
    """List all previews across orgs.

    Superadmin sees all previews. Regular users see previews for their orgs only.
    """
    if user.is_superadmin:
        result = await get_preview_list_base(include_docker_status=status)
    else:
        from app.database import get_org_member, list_user_projects
        # Gather previews across all user's orgs
        user_orgs = await list_user_organizations(user.id)
        all_previews = []
        for org in user_orgs:
            org_result = await get_preview_list_base(include_docker_status=status, org_id=org["id"])
            # Check if user is a real org member or project-only
            org_membership = await get_org_member(user.id, org["id"])
            if org_membership:
                all_previews.extend(org_result["previews"])
            else:
                # Project-only: filter to accessible projects
                accessible = await list_user_projects(org["id"], user.id)
                accessible_slugs = {p["slug"] for p in accessible}
                all_previews.extend(
                    p for p in org_result["previews"] if p.get("project_slug") in accessible_slugs
                )
        result = {"previews": all_previews, "total": len(all_previews)}

    return result


# ---------------------------------------------------------------------------
# Org-scoped preview list
# ---------------------------------------------------------------------------


@router.get("/previews")
async def list_org_previews(
    project: str,
    status: bool = True,
    user: UserWithContext = Depends(require_project_role(OrgRole.viewer)),
):
    """List previews for a specific project in the org."""
    proj = await _resolve_project(user, project)
    project_id = proj["id"]

    # Get all previews for this org, then filter by project
    result = await get_preview_list_base(include_docker_status=status, org_id=user.org.id)
    result["previews"] = [p for p in result["previews"] if p["project"] == proj["slug"]]
    result["total"] = len(result["previews"])
    return result


# ---------------------------------------------------------------------------
# REST endpoints for CLI / actions
# ---------------------------------------------------------------------------


async def _get_executor(project_id: int, preview_name: str, project_slug: str) -> tuple[RemoteExecutor, dict]:
    """Get a RemoteExecutor for the preview's VM. Raises 503 if no VM."""
    preview = await get_preview(project_id, preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail=f"Preview {project_slug}/{preview_name} not found")
    if not preview.get("vm_id") or not preview.get("vm_ip"):
        raise HTTPException(status_code=503, detail="Preview VM is not running. Visit the preview URL to wake it up.")
    return RemoteExecutor(preview["vm_ip"]), preview


@router.post("/previews/{preview_name}/stop")
async def stop_preview(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Stop a preview (shutdown VM, keep disk intact)."""
    proj = await _resolve_project(user, project)
    preview = await get_preview(proj["id"], preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail="Preview not found")

    if not preview.get("vm_id"):
        return {"success": True, "output": "Preview already stopped", "error": ""}

    try:
        await cloud_manager.shutdown_vm(preview["vm_id"])
        return {"success": True, "output": "VM shutdown", "error": ""}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e)}


@router.post("/previews/{preview_name}/start")
async def start_preview(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Start a preview (power on shutdown VM)."""
    proj = await _resolve_project(user, project)
    preview = await get_preview(proj["id"], preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail="Preview not found")

    if not preview.get("vm_id"):
        return {"success": False, "output": "", "error": "No VM found -- use rebuild to create a new one"}

    try:
        vm_ip = await cloud_manager.power_on_vm(preview["vm_id"])
        await cloud_manager.wait_for_vm_ready(preview["vm_id"], timeout=120)

        # Start containers
        executor = RemoteExecutor(vm_ip)
        proc = await executor.run_shell(
            f"cd {VM_PREVIEW_DIR}/code && docker compose up -d"
        )
        await proc.communicate()

        await update_preview_vm(preview["id"], preview["vm_id"], vm_ip)
        return {"success": True, "output": f"VM powered on (IP: {vm_ip})", "error": ""}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e)}


@router.post("/previews/{preview_name}/restart")
async def restart_preview(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Restart containers on the VM."""
    proj = await _resolve_project(user, project)
    executor, preview = await _get_executor(proj["id"], preview_name, proj["slug"])
    try:
        proc = await executor.run_shell(
            "cd /var/www/preview/code && docker compose restart"
        )
        stdout, stderr = await proc.communicate()
        success = proc.returncode == 0
        return {
            "success": success,
            "output": stdout.decode() if isinstance(stdout, bytes) else stdout,
            "error": stderr.decode() if isinstance(stderr, bytes) else stderr if not success else "",
        }
    except Exception as e:
        return {"success": False, "output": "", "error": str(e)}


@router.post("/previews/{preview_name}/drush-uli")
async def drush_uli(
    project: str,
    preview_name: str,
    body: dict = {},
    user: UserWithContext = Depends(require_project_role(OrgRole.viewer)),
):
    """Get a one-time login link (drush uli) via SSH.

    Optional body: {"uri": "https://admin--hash.{preview_domain}"}
    If not provided, uses the default preview URL.
    """
    proj = await _resolve_project(user, project)
    executor, preview = await _get_executor(proj["id"], preview_name, proj["slug"])

    # Use provided URI or fall back to default preview URL
    url_hash = preview.get("url_hash", compute_url_hash(user.org.slug, proj["slug"], preview_name))
    drush_uri = body.get("uri") if body else None
    if not drush_uri:
        drush_uri = f"https://{url_hash}.{settings.preview_domain}"

    php_container = f"{container_prefix(url_hash)}-php"

    proc = await executor.run_shell(
        f"docker exec {php_container} vendor/bin/drush uli --uri={drush_uri}"
    )
    stdout, stderr = await proc.communicate()
    success = proc.returncode == 0
    return {
        "success": success,
        "output": stdout.decode().strip() if isinstance(stdout, bytes) else stdout,
        "error": stderr.decode() if isinstance(stderr, bytes) else stderr if not success else "",
    }


@router.post("/previews/{preview_name}/drush")
async def drush_command(
    project: str,
    preview_name: str,
    request: Request,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Run an arbitrary drush command via SSH."""
    body = await request.json()
    args_str = body.get("args", "")
    if not args_str:
        raise HTTPException(status_code=400, detail="Missing 'args' in request body")

    proj = await _resolve_project(user, project)
    executor, preview = await _get_executor(proj["id"], preview_name, proj["slug"])
    url_hash = preview.get("url_hash", compute_url_hash(user.org.slug, proj["slug"], preview_name))
    php_container = f"{container_prefix(url_hash)}-php"

    proc = await executor.run_shell(
        f"docker exec {php_container} vendor/bin/drush {args_str}"
    )
    stdout, stderr = await proc.communicate()
    success = proc.returncode == 0
    return {
        "success": success,
        "output": stdout.decode() if isinstance(stdout, bytes) else stdout,
        "error": stderr.decode() if isinstance(stderr, bytes) else stderr if not success else "",
    }


@router.post("/previews/{preview_name}/rebuild")
async def rebuild_preview(
    request: Request,
    project: str,
    preview_name: str,
    force_new: bool = False,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Re-clone the preview from GitLab (internal rebuild)."""
    proj = await _resolve_project(user, project)
    project_id = proj["id"]
    project_slug = proj["slug"]

    state = await PreviewStateManager.load_state(project_id, preview_name)
    if not state:
        raise HTTPException(status_code=404, detail="Preview not found")
    if not state.get("branch"):
        raise HTTPException(status_code=400, detail="Cannot determine branch for this preview")

    project_path = proj.get("gitlab_project_path")
    if not project_path:
        raise HTTPException(status_code=400, detail=f"Project '{project_slug}' has no GitLab project path configured")

    # If a deploy is currently running, cancel it so the new one can start
    from app.valkey import is_deploy_locked, request_deploy_cancel
    deploy_key = f"{project_slug}/{preview_name}"
    if await is_deploy_locked(deploy_key):
        await request_deploy_cancel(deploy_key)
        logger.info(f"Cancelling active deploy for {deploy_key} — rebuild requested")

        # Also cancel the deploy on the VM agent directly
        vm_ip = state.get("vm_ip")
        if vm_ip:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(f"http://{vm_ip}:8022/deploy/cancel")
                    logger.info(f"Sent cancel to VM agent at {vm_ip}")
            except Exception as e:
                logger.warning(f"Failed to cancel on VM agent: {e}")

        # Mark any running deployment as cancelled in DB
        preview = await get_preview(project_id, preview_name)
        if preview:
            from app.database import get_running_deployment
            existing = await get_running_deployment(preview["id"])
            if existing:
                await finish_deployment(
                    existing["id"], "cancelled",
                    error="Cancelled by new deploy request",
                )
                from app.websockets import deployment_log_broadcaster
                await deployment_log_broadcaster.complete(existing["id"], False)

    trigger_type = "rebuild" if force_new else "update"
    logger.info(
        f"Enqueued task_deploy_preview: {project_slug}/{preview_name} "
        f"triggered_by={trigger_type} user={user.email} branch={state['branch']} force_new={force_new}"
    )
    await request.app.state.arq.enqueue_job(
        "task_deploy_preview",
        user.org.id, user.org.slug, project_id, project_slug, project_path,
        preview_name, state["branch"], state.get("commit_sha", ""),
        trigger_type,
        state.get("mr_id"),
        force_new,
        state.get("mr_title"),
        state.get("target_branch"),
        _expires=timedelta(hours=3),
    )

    return {
        "success": True,
        "output": f"Rebuild started for {project_slug}/{preview_name} (branch: {state['branch']}, force_new={force_new})",
        "error": "",
    }


@router.post("/previews/{preview_name}/terminal-token")
async def get_terminal_token(
    project: str,
    preview_name: str,
    body: dict = {},
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Generate a short-lived HMAC token for direct terminal access on the VM."""
    import hashlib
    import hmac as hmac_mod
    import time as time_mod

    proj = await _resolve_project(user, project)
    project_slug = proj["slug"]
    container = body.get("container", "php")

    # Derive the terminal secret (same as what the VM terminal server has)
    terminal_secret = hmac_mod.new(
        settings.secret_key.encode(),
        f"terminal:{user.org.slug}:{project_slug}:{preview_name}".encode(),
        hashlib.sha256,
    ).hexdigest()

    # Build token: "container_name:expiry:hmac"
    prefix = f"{preview_name}-{project_slug}"
    container_name = f"{prefix}-{container}"
    exp = int(time_mod.time()) + 60
    payload = f"{container_name}:{exp}"
    token_hmac = hmac_mod.new(
        terminal_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()

    return {"token": f"{payload}:{token_hmac}", "container": container_name}


@router.post("/previews/{preview_name}/rerun-post-deploy")
async def rerun_post_deploy(
    request: Request,
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Re-run the post-deploy script for this preview."""
    proj = await _resolve_project(user, project)
    project_id = proj["id"]
    project_slug = proj["slug"]

    state = await PreviewStateManager.load_state(project_id, preview_name)
    if not state:
        raise HTTPException(status_code=404, detail="Preview not found")
    if not state.get("vm_ip"):
        raise HTTPException(status_code=400, detail="No VM found for this preview")

    await request.app.state.arq.enqueue_job(
        "task_run_post_deploy",
        user.org.slug,
        project_id,
        project_slug,
        preview_name,
        user.email,
    )

    return {"success": True, "message": f"Post-deploy started for {project_slug}/{preview_name}"}


@router.get("/previews/{preview_name}/deployments")
async def list_preview_deployments(
    project: str,
    preview_name: str,
    limit: int = 50,
    user: UserWithContext = Depends(require_project_role(OrgRole.viewer)),
):
    proj = await _resolve_project(user, project)
    preview = await get_preview(proj["id"], preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail=f"Preview {project}/{preview_name} not found")
    deployments = await db_list_deployments(preview["id"], limit=limit)
    # Parse phases JSON string into list for each deployment
    for d in deployments:
        if d.get("phases") and isinstance(d["phases"], str):
            try:
                d["phases"] = json.loads(d["phases"])
            except (json.JSONDecodeError, TypeError):
                d["phases"] = None
    return {"deployments": deployments, "total": len(deployments)}


@router.get("/previews/{preview_name}/deployments/{deployment_id}")
async def get_preview_deployment(
    project: str,
    preview_name: str,
    deployment_id: int,
    user: UserWithContext = Depends(require_project_role(OrgRole.viewer)),
):
    deployment = await db_get_deployment(deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")
    proj = await _resolve_project(user, project)
    preview = await get_preview(proj["id"], preview_name)
    if not preview or deployment["preview_id"] != preview["id"]:
        raise HTTPException(status_code=404, detail="Deployment not found for this preview")

    # Parse phases JSON
    if deployment.get("phases") and isinstance(deployment["phases"], str):
        try:
            deployment["phases"] = json.loads(deployment["phases"])
        except (json.JSONDecodeError, TypeError):
            deployment["phases"] = None

    # Check if log stream is still active in Valkey (e.g. post-deploy running)
    try:
        from app.valkey import get_deploy_complete, deploy_log_exists
        complete = await get_deploy_complete(deployment_id)
        has_buffer = await deploy_log_exists(deployment_id)
        # Stream is active only if there's a buffer AND no complete flag yet
        deployment["stream_active"] = has_buffer and complete is None
    except Exception:
        deployment["stream_active"] = False

    return deployment


@router.get("/previews/{preview_name}/deployments/{deployment_id}/live-logs")
async def get_deployment_live_logs(
    project: str,
    preview_name: str,
    deployment_id: int,
    offset: int = 0,
    user: UserWithContext = Depends(require_project_role(OrgRole.viewer)),
):
    from app.valkey import get_deploy_log_buffer, get_deploy_complete

    proj = await _resolve_project(user, project)
    preview = await get_preview(proj["id"], preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail="Preview not found")

    # Try Valkey buffer first (active or recently completed deployment)
    try:
        from app.valkey import deploy_log_exists
        complete = await get_deploy_complete(deployment_id)
        has_buffer = await deploy_log_exists(deployment_id)
        # If deploy is tracked in Valkey (buffer key exists or complete flag set), use it
        if has_buffer or complete is not None:
            buffered = await get_deploy_log_buffer(deployment_id) if has_buffer else []
            lines = buffered[offset:]
            is_complete = complete is not None
            return {
                "lines": lines,
                "offset": offset + len(lines),
                "complete": is_complete,
                "status": "complete" if is_complete else "running",
            }
    except Exception:
        pass

    # Fall back to DB for historical deployments
    deployment = await db_get_deployment(deployment_id)
    if not deployment or deployment["preview_id"] != preview["id"]:
        raise HTTPException(status_code=404, detail="Deployment not found")

    log_output = deployment.get("log_output") or ""
    lines = [log_output] if offset == 0 and log_output else []
    return {
        "lines": lines,
        "offset": offset + len(lines),
        "complete": True,
        "status": deployment.get("status", "unknown"),
    }


@router.get("/previews/{preview_name}/db/download")
async def download_db(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Stream a gzipped SQL dump from the preview VM."""
    proj = await _resolve_project(user, project)
    executor, preview = await _get_executor(proj["id"], preview_name, proj["slug"])
    url_hash = preview.get("url_hash", compute_url_hash(user.org.slug, proj["slug"], preview_name))
    php_container = f"{container_prefix(url_hash)}-php"

    async def generate():
        proc = await executor.run_shell(
            f"docker exec {php_container} vendor/bin/drush sql-dump | gzip"
        )
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
        await proc.wait()

    filename = f"{proj['slug']}-{preview_name}.sql.gz"
    return StreamingResponse(
        generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/previews/{preview_name}/files/download")
async def download_files(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Stream a tar.gz of the preview's files directory from the VM."""
    proj = await _resolve_project(user, project)
    executor, preview = await _get_executor(proj["id"], preview_name, proj["slug"])

    tar_excludes = "--exclude=./css --exclude=./js --exclude=./php"
    files_dir = "/var/www/preview/code/web/sites/default/files"

    async def generate():
        proc = await executor.run_shell(
            f"tar czf - {tar_excludes} -C {files_dir} ."
        )
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
        await proc.wait()

    filename = f"{proj['slug']}-{preview_name}-files.tar.gz"
    return StreamingResponse(
        generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/previews/{preview_name}/stats")
async def preview_vm_stats(
    project: str,
    preview_name: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.viewer)),
):
    """Get CPU, RAM and disk stats from the preview's VM."""
    proj = await _resolve_project(user, project)
    preview = await get_preview(proj["id"], preview_name)
    if not preview:
        raise HTTPException(status_code=404, detail="Preview not found")
    if not preview.get("vm_id") or not preview.get("vm_ip"):
        return {"status": "unavailable", "reason": "VM not ready"}
    executor = RemoteExecutor(preview["vm_ip"])

    script = (
        "import json, os, time\n"
        "def read_cpu():\n"
        "    lines = open('/proc/stat').readlines()\n"
        "    c = lines[0].split()[1:]\n"
        "    return [int(x) for x in c]\n"
        "c1 = read_cpu()\n"
        "time.sleep(0.5)\n"
        "c2 = read_cpu()\n"
        "d = [b - a for a, b in zip(c1, c2)]\n"
        "total = sum(d)\n"
        "idle = d[3] + d[4]\n"
        "cpu_pct = round((total - idle) / total * 100, 1) if total > 0 else 0.0\n"
        "mem = open('/proc/meminfo').read()\n"
        "mt = int([l for l in mem.splitlines() if l.startswith('MemTotal')][0].split()[1]) * 1024\n"
        "ma = int([l for l in mem.splitlines() if l.startswith('MemAvailable')][0].split()[1]) * 1024\n"
        "st = os.statvfs('/')\n"
        "dt = st.f_blocks * st.f_frsize\n"
        "du = (st.f_blocks - st.f_bfree) * st.f_frsize\n"
        "ncpu = os.cpu_count()\n"
        "print(json.dumps({\n"
        "    'memory_total_gb': round(mt/1073741824, 2),\n"
        "    'memory_available_gb': round(ma/1073741824, 2),\n"
        "    'memory_percent': round((mt - ma) / mt * 100, 1),\n"
        "    'disk_total_gb': round(dt/1073741824, 2),\n"
        "    'disk_used_gb': round(du/1073741824, 2),\n"
        "    'disk_percent': round(du / dt * 100, 1),\n"
        "    'cpu_percent': cpu_pct,\n"
        "    'cpu_count': ncpu\n"
        "}))\n"
    )
    import base64
    encoded = base64.b64encode(script.encode()).decode()
    cmd = f"echo {encoded} | base64 -d | python3"
    try:
        proc = await executor.run_shell(cmd)
        stdout, stderr = await proc.communicate()
    except Exception:
        return {"status": "unavailable", "reason": "VM not reachable"}
    if proc.returncode != 0:
        return {"status": "unavailable", "reason": "VM not reachable"}

    try:
        data = json.loads(stdout.decode().strip())
        data["status"] = "ok"
        return data
    except Exception:
        return {"status": "unavailable", "reason": "Failed to parse stats"}
