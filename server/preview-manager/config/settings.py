from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Preview Manager configuration"""

    # API Settings
    api_host: str = "0.0.0.0"  # Listen on all interfaces (allows Docker containers to connect)
    api_port: int = 8000

    # Preview Settings
    previews_base_path: str = "/var/www/previews"
    inactivity_threshold_minutes: int = 15

    # Resource Monitoring
    max_memory_percent: float = 85.0  # Sleep previews if RAM > 85%
    max_cpu_percent: float = 90.0     # Sleep previews if CPU > 90%
    check_interval_seconds: int = 60   # Check every 60 seconds

    # Docker Compose Settings
    docker_network: str = "preview-network"
    drupal_base_image: str = "preview-drupal"
    docker_registry: str = ""  # e.g. "localhost:5000" — if set, all images are pulled from this registry
    default_php_version: str = "8.3"
    default_mysql_version: str = "8.0"

    # GitLab Integration
    gitlab_url: str = "https://gitlab.com"
    gitlab_webhook_secret: str = ""

    # Database
    database_url: str = "postgresql://preview_manager:preview_manager@localhost:5432/preview_manager"
    valkey_url: str = "redis://localhost:6379"

    secret_key: str = "change-me-in-production"
    gitlab_oauth_client_id: str = ""
    gitlab_oauth_client_secret: str = ""
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    oauth_redirect_uri_base: str = "https://api.preview-mr.com/api/auth/callback"

    # GitLab API access (Personal Access Token)
    gitlab_oauth_access_token: Optional[str] = None
    session_max_age_seconds: int = 604800  # 7 days
    frontend_url: str = "https://app.preview-mr.com"

    # Resend (email)
    resend_api_key: str = ""
    invitation_from_email: str = "Preview Manager <noreply@preview-mr.com>"

    # Hetzner Cloud
    hetzner_api_token: str = ""
    hetzner_location: str = "fsn1"
    hetzner_server_type: str = "cx23"
    hetzner_snapshot_id: int = 0
    hetzner_ssh_private_key_path: str = ""
    hetzner_ssh_public_key: str = ""

    # Warm pool — pre-created VMs ready for instant assignment
    warm_pool_size: int = 1  # Number of VMs to keep ready in the pool

    # Uvicorn
    uvicorn_workers: int = 2

    # Composer proxy (internal tinyproxy for private registries)
    composer_proxy_url: str = ""

    # Storage backend: "s3" or "storagebox"
    storage_backend: str = "s3"

    # Hetzner Object Storage (S3)
    hetzner_s3_endpoint: str = ""
    hetzner_s3_access_key: str = ""
    hetzner_s3_secret_key: str = ""
    hetzner_s3_bucket: str = "preview-manager"

    # Hetzner Storage Box (SFTP)
    storagebox_host: str = ""
    storagebox_port: int = 23
    storagebox_user: str = ""
    storagebox_password: str = ""
    storagebox_ssh_key_path: str = ""
    storagebox_base_path: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
