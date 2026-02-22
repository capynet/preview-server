"""CLI distribution endpoints"""

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cli", tags=["cli"])

CLI_DIR = Path("/var/www/preview-manager/cli")
INSTALL_SCRIPT = CLI_DIR / "install.sh"
VERSION_FILE = CLI_DIR / "VERSION"

VALID_OS = {"linux", "darwin"}
VALID_ARCH = {"amd64", "arm64"}


@router.get("/version")
async def get_cli_version():
    """Return the latest published CLI version."""
    if not VERSION_FILE.exists():
        return JSONResponse({"error": "Version file not found"}, status_code=404)
    version = VERSION_FILE.read_text().strip()
    return JSONResponse({"version": version})


@router.get("/install.sh")
async def get_install_script():
    """Return the CLI install script."""
    if not INSTALL_SCRIPT.exists():
        return PlainTextResponse("Install script not found", status_code=404)
    return FileResponse(
        INSTALL_SCRIPT,
        media_type="text/plain",
        filename="install.sh",
    )


@router.get("/download/{os}/{arch}")
async def download_binary(os: str, arch: str):
    """Download the CLI binary for a given OS and architecture."""
    if os not in VALID_OS:
        return PlainTextResponse(f"Unsupported OS: {os}", status_code=400)
    if arch not in VALID_ARCH:
        return PlainTextResponse(f"Unsupported architecture: {arch}", status_code=400)

    binary_path = CLI_DIR / f"preview-{os}-{arch}"
    if not binary_path.exists():
        return PlainTextResponse(
            f"Binary not available for {os}/{arch}", status_code=404
        )

    return FileResponse(
        binary_path,
        media_type="application/octet-stream",
        filename="preview",
    )
