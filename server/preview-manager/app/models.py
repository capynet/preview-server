"""Pydantic models for Druploy API"""

from typing import Optional

from pydantic import BaseModel


class PreviewInfo(BaseModel):
    """Preview information response"""
    preview_name: str
    project_slug: str
    org_slug: str
    url_hash: str
    mr_id: Optional[int] = None
    status: str
    url: str
    path: str
    branch: str
    commit_sha: str
    created_at: str
    last_deployed_at: Optional[str] = None
    last_deployment: Optional[dict] = None
    auto_update: bool = True
    pinned: bool = False
    env_vars: dict[str, str] = {}
    mr_title: Optional[str] = None
    mr_url: Optional[str] = None
    gitlab_project_path: Optional[str] = None
    target_branch: Optional[str] = None
    vm_ip: Optional[str] = None
    post_deploy_status: Optional[str] = None
    domain_aliases: dict[str, str] = {}
    exposed_services: dict[str, str] = {}
    stack: dict[str, str] = {}
