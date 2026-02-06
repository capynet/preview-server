"""Pydantic models for Preview Manager API"""

from typing import Optional

from pydantic import BaseModel


class DeployRequest(BaseModel):
    """Request to deploy a preview"""
    project: str
    mr_id: int
    commit_sha: str
    branch: str
    repo_url: str
    repo_path: Optional[str] = None
    triggered_by: Optional[str] = "api"


class DeployResponse(BaseModel):
    """Response from deployment"""
    success: bool
    preview_name: str
    preview_url: str
    preview_path: str
    status: str
    duration_seconds: Optional[int] = None
    message: str
    error: Optional[str] = None


class PreviewState(BaseModel):
    """Preview state stored in JSON file"""
    mr_id: int
    project: str
    branch: str
    commit_sha: str
    status: str  # creating, active, failed
    url: str
    path: str
    created_at: str
    last_deployed_at: Optional[str] = None
    last_deployment: Optional[dict] = None


class PreviewInfo(BaseModel):
    """Preview information response"""
    preview_name: str
    project: str
    mr_id: int
    status: str
    url: str
    path: str
    branch: str
    commit_sha: str
    created_at: str
    last_deployed_at: Optional[str] = None
    last_deployment: Optional[dict] = None
    mr_title: Optional[str] = None
    mr_url: Optional[str] = None
