"""Deployment execution logic"""

import asyncio
import logging
import subprocess
from datetime import datetime
from pathlib import Path

from app.models import DeployRequest, DeployResponse, PreviewState
from app.state import PreviewStateManager

logger = logging.getLogger(__name__)


class DeploymentExecutor:
    """Execute deployment scripts"""

    # Core scripts to execute in order
    CORE_SCRIPTS = [
        "00-validate-requirements.sh",
        "01-detect-preview.sh",
        "02-sync-repository.sh",
        "03-configure-ddev.sh",
        "05-import-database.sh",
        "04-run-deployment.sh",
        "06-print-summary.sh"
    ]

    @staticmethod
    def get_scripts_path() -> Path:
        """Get path to core scripts"""
        return Path("/var/www/preview-manager/scripts/core")

    @staticmethod
    def prepare_environment(project: str, mr_id: int, request: DeployRequest) -> dict:
        """Prepare environment variables for scripts"""
        import os

        preview_name = f"mr-{mr_id}"
        preview_path = PreviewStateManager.get_preview_path(project, mr_id)
        # URL structure: https://mr-{id}-{project}.mr.preview-mr.com
        preview_url = f"https://{preview_name}-{project}.mr.preview-mr.com"

        env = os.environ.copy()
        env.update({
            # Preview info
            "PROJECT_NAME": project,
            "PREVIEW_NAME": preview_name,
            "PREVIEW_DIR": str(preview_path),
            "PREVIEW_URL": preview_url,

            # Git info
            "CI_COMMIT_SHA": request.commit_sha,
            "CI_COMMIT_SHORT_SHA": request.commit_sha[:8],
            "CI_COMMIT_REF_NAME": request.branch,
            "CI_MERGE_REQUEST_IID": str(mr_id),

            # Paths
            "CI_PROJECT_DIR": request.repo_path or f"/tmp/repo-{request.commit_sha}",

            # Repository
            "CI_REPOSITORY_URL": request.repo_url,

            # Defaults
            "DB_SOURCE": "/backups/drupal-base.sql.gz",
            "FILES_SOURCE": "/backups/drupal-files.tar.gz",
            "DRUPAL_VERSION": "drupal10",
            "DOCROOT": "web",
        })

        return env

    @staticmethod
    async def execute_deployment(project: str, mr_id: int, request: DeployRequest) -> DeployResponse:
        """
        Execute deployment

        Returns:
            DeployResponse with result
        """
        preview_name = f"mr-{mr_id}"
        preview_path = PreviewStateManager.get_preview_path(project, mr_id)
        # URL structure: https://mr-{id}-{project}.mr.preview-mr.com
        preview_url = f"https://{preview_name}-{project}.mr.preview-mr.com"

        logger.info(f"Starting deployment for {project}/mr-{mr_id}")

        # Create initial state
        state = PreviewState(
            mr_id=mr_id,
            project=project,
            branch=request.branch,
            commit_sha=request.commit_sha,
            status="creating",
            url=preview_url,
            path=str(preview_path),
            created_at=datetime.utcnow().isoformat(),
        )
        PreviewStateManager.save_state(project, mr_id, state)

        # Prepare environment
        env = DeploymentExecutor.prepare_environment(project, mr_id, request)

        # Ensure preview directory exists
        preview_path.mkdir(parents=True, exist_ok=True)

        deployment_start = datetime.utcnow()
        logs = []

        try:
            scripts_path = DeploymentExecutor.get_scripts_path()

            # Execute each script
            for script_name in DeploymentExecutor.CORE_SCRIPTS:
                script_path = scripts_path / script_name

                if not script_path.exists():
                    logger.warning(f"Script not found: {script_path}, skipping")
                    continue

                logger.info(f"Executing {script_name}")

                try:
                    result = await asyncio.to_thread(
                        subprocess.run,
                        [str(script_path)],
                        cwd=str(preview_path),
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=600  # 10 minutes per script
                    )

                    output = result.stdout + result.stderr
                    logs.append(f"\n{'='*60}\n{script_name}\n{'='*60}\n{output}")

                    if result.returncode != 0:
                        error_msg = f"Script {script_name} failed with exit code {result.returncode}"
                        logger.error(error_msg)
                        logger.error(f"Script output:\n{output}")

                        # Update state to failed
                        state.status = "failed"
                        state.last_deployment = {
                            "commit_sha": request.commit_sha,
                            "status": "failed",
                            "error": error_msg,
                            "logs": "\n".join(logs),
                            "completed_at": datetime.utcnow().isoformat(),
                        }
                        PreviewStateManager.save_state(project, mr_id, state)

                        return DeployResponse(
                            success=False,
                            preview_name=preview_name,
                            preview_url=preview_url,
                            preview_path=str(preview_path),
                            status="failed",
                            message="Deployment failed",
                            error=f"{error_msg}\n\nOutput:\n{output}"
                        )

                    logger.info(f"Script {script_name} completed successfully")

                except subprocess.TimeoutExpired:
                    error_msg = f"Script {script_name} timed out after 10 minutes"
                    logger.error(error_msg)

                    state.status = "failed"
                    state.last_deployment = {
                        "commit_sha": request.commit_sha,
                        "status": "failed",
                        "error": error_msg,
                        "completed_at": datetime.utcnow().isoformat(),
                    }
                    PreviewStateManager.save_state(project, mr_id, state)

                    return DeployResponse(
                        success=False,
                        preview_name=preview_name,
                        preview_url=preview_url,
                        preview_path=str(preview_path),
                        status="failed",
                        message="Deployment failed",
                        error=error_msg
                    )

            # All scripts completed successfully
            deployment_end = datetime.utcnow()
            duration = int((deployment_end - deployment_start).total_seconds())

            state.status = "active"
            state.last_deployed_at = deployment_end.isoformat()
            state.last_deployment = {
                "commit_sha": request.commit_sha,
                "status": "completed",
                "duration_seconds": duration,
                "completed_at": deployment_end.isoformat(),
            }
            PreviewStateManager.save_state(project, mr_id, state)

            logger.info(f"Deployment completed successfully in {duration}s")

            return DeployResponse(
                success=True,
                preview_name=preview_name,
                preview_url=preview_url,
                preview_path=str(preview_path),
                status="active",
                duration_seconds=duration,
                message=f"Deployment completed successfully in {duration}s"
            )

        except Exception as e:
            logger.error(f"Deployment error: {e}", exc_info=True)

            state.status = "failed"
            state.last_deployment = {
                "commit_sha": request.commit_sha,
                "status": "failed",
                "error": str(e),
                "completed_at": datetime.utcnow().isoformat(),
            }
            PreviewStateManager.save_state(project, mr_id, state)

            return DeployResponse(
                success=False,
                preview_name=preview_name,
                preview_url=preview_url,
                preview_path=str(preview_path),
                status="failed",
                message="Deployment failed",
                error=str(e)
            )
