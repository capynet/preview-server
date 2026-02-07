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

    # Traefik Logs
    traefik_container_name: str = "traefik-traefik-1"

    # DDEV Settings
    ddev_binary: str = "/usr/bin/ddev"

    # GitLab Integration
    gitlab_url: str = "https://gitlab.com"
    gitlab_api_token: Optional[str] = None
    gitlab_group_name: str = "preview-tests"
    pipeline_check_interval_seconds: int = 5  # How often to check for active pipelines (WebSocket)

    # Auth
    auth_db_path: str = "/var/www/preview-manager/auth.db"
    secret_key: str = "change-me-in-production"
    gitlab_oauth_client_id: str = ""
    gitlab_oauth_client_secret: str = ""
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    oauth_redirect_uri_base: str = "https://api.preview-mr.com/api/auth/callback"

    # GitLab Connect OAuth (separate app with `api` scope for previews)
    gitlab_connect_client_id: str = ""
    gitlab_connect_client_secret: str = ""
    # Stored after OAuth connect flow
    gitlab_oauth_access_token: Optional[str] = None
    gitlab_oauth_refresh_token: Optional[str] = None
    gitlab_oauth_token_expires_at: Optional[int] = None
    allowed_email_domains: str = ""  # comma-separated
    session_max_age_seconds: int = 604800  # 7 days
    frontend_url: str = "https://app.preview-mr.com"

    # Resend (email)
    resend_api_key: str = ""
    invitation_from_email: str = "Preview Manager <noreply@preview-mr.com>"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
