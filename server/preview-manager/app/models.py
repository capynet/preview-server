"""Pydantic models for Preview Manager API"""

from typing import Optional

from pydantic import BaseModel


class PreviewInfo(BaseModel):
    """Preview information response"""
    preview_name: str
    project: str
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
    mr_title: Optional[str] = None
    mr_url: Optional[str] = None
