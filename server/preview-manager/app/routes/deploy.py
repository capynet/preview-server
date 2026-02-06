"""Deploy endpoint"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.models import DeployRequest, DeployResponse
from app.deployment import DeploymentExecutor
from app.auth.dependencies import require_role
from app.auth.models import Role, UserWithRole

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/deploy", response_model=DeployResponse)
async def deploy_preview(request: DeployRequest, user: UserWithRole = Depends(require_role(Role.member))):
    """
    Deploy a preview environment

    Creates/updates preview directory, executes deployment scripts,
    and saves state to JSON file.
    """
    logger.info(
        f"Deploy request: project={request.project}, mr={request.mr_id}, "
        f"commit={request.commit_sha[:8]}, branch={request.branch}"
    )

    try:
        result = await DeploymentExecutor.execute_deployment(
            request.project,
            request.mr_id,
            request
        )
        return result

    except Exception as e:
        logger.error(f"Error deploying preview: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
