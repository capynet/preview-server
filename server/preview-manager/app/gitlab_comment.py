"""Post/update a comment on a GitLab MR after successful preview deploy."""

import json
import logging
from datetime import datetime, timezone

import httpx

from app.database import get_preview, get_project, get_pool
from config.settings import settings

logger = logging.getLogger(__name__)


async def post_or_update_mr_comment(
    org_id: int,
    project_id: int,
    preview_name: str,
    mr_iid: int,
    preview_url: str,
    url_hash: str,
    branch: str,
    commit_sha: str,
    deploy_duration: int,
    stack_info: dict | None = None,
    domain_aliases: list[str] | None = None,
) -> None:
    """Create or update a GitLab MR note with preview info.

    Non-fatal: logs errors but never raises.
    """
    try:
        await _post_or_update(
            org_id, project_id, preview_name, mr_iid,
            preview_url, url_hash, branch, commit_sha,
            deploy_duration, stack_info, domain_aliases,
        )
    except Exception as e:
        logger.warning(f"Failed to post MR comment for {preview_name}: {e}")


async def _post_or_update(
    org_id: int,
    project_id: int,
    preview_name: str,
    mr_iid: int,
    preview_url: str,
    url_hash: str,
    branch: str,
    commit_sha: str,
    deploy_duration: int,
    stack_info: dict | None,
    domain_aliases: list[str] | None,
) -> None:
    from app.routes.gitlab import _get_org_gitlab_token

    gitlab_url, token = await _get_org_gitlab_token(org_id)
    project = await get_project(project_id)
    if not project or not project.get("gitlab_project_path"):
        logger.warning(f"Cannot post MR comment: project {project_id} has no gitlab_project_path")
        return

    encoded_path = project["gitlab_project_path"].replace("/", "%2F")
    project_slug = project["slug"]

    # Build comment body
    body = _build_comment(
        preview_url=preview_url,
        url_hash=url_hash,
        project_slug=project_slug,
        branch=branch,
        commit_sha=commit_sha,
        deploy_duration=deploy_duration,
        stack_info=stack_info,
        domain_aliases=domain_aliases,
    )

    # Check if we already have a note_id for this preview
    preview = await get_preview(project_id, preview_name)
    note_id = preview.get("gitlab_note_id") if preview else None

    async with httpx.AsyncClient(timeout=15) as client:
        if note_id:
            # Try to update existing note
            resp = await client.put(
                f"{gitlab_url}/api/v4/projects/{encoded_path}/merge_requests/{mr_iid}/notes/{note_id}",
                headers={"PRIVATE-TOKEN": token},
                json={"body": body},
            )
            if resp.status_code == 404:
                # Note was deleted, create a new one
                note_id = None
            elif resp.status_code >= 400:
                logger.warning(f"Failed to update MR note {note_id}: HTTP {resp.status_code}")
                return

        if not note_id:
            # Create new note
            resp = await client.post(
                f"{gitlab_url}/api/v4/projects/{encoded_path}/merge_requests/{mr_iid}/notes",
                headers={"PRIVATE-TOKEN": token},
                json={"body": body},
            )
            if resp.status_code >= 400:
                logger.warning(f"Failed to create MR note: HTTP {resp.status_code} - {resp.text}")
                return
            note_id = resp.json().get("id")

    # Save note_id to DB
    if note_id and preview:
        pool = await get_pool()
        await pool.execute(
            "UPDATE previews SET gitlab_note_id = $1 WHERE id = $2",
            note_id, preview["id"],
        )
        logger.info(f"MR comment posted/updated for {preview_name} (note_id={note_id})")


def _build_comment(
    *,
    preview_url: str,
    url_hash: str,
    project_slug: str,
    branch: str,
    commit_sha: str,
    deploy_duration: int,
    stack_info: dict | None,
    domain_aliases: list[str] | None,
) -> str:
    """Build the markdown comment body."""
    lines = ["## :rocket: Preview Ready", ""]

    # Main info table
    lines.append("| Info | |")
    lines.append("|---|---|")
    lines.append(f"| **URL** | [{url_hash}.{settings.preview_domain}]({preview_url}) |")

    dashboard_url = f"{settings.frontend_url}/projects/{project_slug}"
    lines.append(f"| **Dashboard** | [View in Druploy]({dashboard_url}) |")

    # Stack info
    if stack_info:
        stack_parts = []
        if stack_info.get("php"):
            stack_parts.append(f"PHP {stack_info['php']}")
        if stack_info.get("database"):
            stack_parts.append(stack_info["database"])
        if stack_info.get("redis"):
            stack_parts.append(f"Redis {stack_info['redis']}")
        if stack_info.get("solr"):
            stack_parts.append(f"Solr {stack_info['solr']}")
        if stack_info.get("valkey"):
            stack_parts.append(f"Valkey {stack_info['valkey']}")
        if stack_parts:
            lines.append(f"| **Stack** | {' · '.join(stack_parts)} |")

    lines.append("")

    # Domain aliases (multisite)
    if domain_aliases:
        lines.append("**Sites:**")
        lines.append(f"- [default]({preview_url})")
        for alias in domain_aliases:
            alias_url = f"https://{alias}--{url_hash}.{settings.preview_domain}"
            lines.append(f"- [{alias}]({alias_url})")
        lines.append("")

    # Footer
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    duration_str = _fmt_duration(deploy_duration)
    lines.append("---")
    lines.append(f"*Deploy: {commit_sha[:8]} · {duration_str} · Updated: {now}*")

    return "\n".join(lines)


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s"


