"""Docker Compose generator for preview environments."""

import logging
from pathlib import Path
from typing import Any

import yaml

from config.settings import settings

logger = logging.getLogger(__name__)

# Defaults when preview.yml is missing or incomplete
DEFAULTS = {
    "php_version": "8.3",
    "database": "mysql:8.0",
    "docroot": "web",
    "services": {
        "redis": False,
        "solr": False,
    },
    "env": {},
    "deploy": {
        "new": None,
        "update": None,
    },
}


def parse_preview_yml(preview_path: Path) -> dict:
    """Read and validate preview.yml from the project root, applying defaults."""
    config = dict(DEFAULTS)
    config["services"] = dict(DEFAULTS["services"])
    config["env"] = dict(DEFAULTS["env"])
    config["deploy"] = dict(DEFAULTS["deploy"])

    yml_file = preview_path / "preview.yml"
    if not yml_file.exists():
        logger.info(f"No preview.yml found at {yml_file}, using defaults")
        return config

    try:
        raw = yaml.safe_load(yml_file.read_text()) or {}
    except Exception as e:
        logger.warning(f"Failed to parse preview.yml: {e}, using defaults")
        return config

    if "php_version" in raw:
        config["php_version"] = str(raw["php_version"])

    # Unified "database" property: "mysql:8.0", "mariadb:10.6", etc.
    # Also supports legacy "mysql_version" and "mariadb" for backwards compat.
    if "database" in raw:
        config["database"] = str(raw["database"])
    elif "mariadb" in raw:
        config["database"] = f"mariadb:{raw['mariadb']}"
    elif "mysql_version" in raw:
        db_val = str(raw["mysql_version"])
        config["database"] = f"mysql:{db_val}" if ":" not in db_val else db_val
    if "docroot" in raw:
        config["docroot"] = str(raw["docroot"])

    if "services" in raw and isinstance(raw["services"], dict):
        for svc in ("redis", "solr"):
            if svc in raw["services"]:
                config["services"][svc] = bool(raw["services"][svc])

    if "env" in raw and isinstance(raw["env"], dict):
        config["env"].update({str(k): str(v) for k, v in raw["env"].items()})

    # Deploy scripts — optional paths, None means no script
    if "deploy" in raw and isinstance(raw["deploy"], dict):
        for phase in ("new", "update"):
            val = raw["deploy"].get(phase)
            if val is False or val is None:
                config["deploy"][phase] = None
            elif isinstance(val, str) and val:
                config["deploy"][phase] = val
    elif "deploy" in raw and raw["deploy"] is False:
        # deploy: false — explicitly disable all deploy scripts
        config["deploy"] = {"new": None, "update": None}

    logger.info(f"Parsed preview.yml: php={config['php_version']}, database={config['database']}, "
                f"redis={config['services']['redis']}, solr={config['services']['solr']}, "
                f"deploy.new={config['deploy']['new']}, deploy.update={config['deploy']['update']}")
    return config


def _container_prefix(project_name: str, preview_name: str) -> str:
    return f"{preview_name}-{project_name}"


def generate_docker_compose(
    project_name: str,
    preview_name: str,
    config: dict,
    branch: str = "",
    commit_sha: str = "",
    mr_iid: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """Generate a docker-compose.yml dict for a preview environment."""
    prefix = _container_prefix(project_name, preview_name)
    domain = f"{prefix}.mr.preview-mr.com"
    url = f"https://{domain}"
    network_name = settings.docker_network

    # Determine DB image from unified "database" property (e.g. "mysql:8.0", "mariadb:10.6")
    db_spec = config["database"]
    if ":" in db_spec:
        db_image = db_spec
    else:
        db_image = f"mysql:{db_spec}"

    # Detect host UID/GID so the container can remap www-data to match,
    # avoiding file ownership conflicts between host and container.
    import os
    host_uid = str(os.getuid())
    host_gid = str(os.getgid())

    # PHP environment — all preview vars use PREV_ prefix
    php_env: dict[str, str] = {
        "HOST_UID": host_uid,
        "HOST_GID": host_gid,
        "PREV_IS_PREVIEW": "true",
        "PREV_PROJECT_NAME": project_name,
        "PREV_PREVIEW_NAME": preview_name,
        "PREV_MR_IID": str(mr_iid) if mr_iid else "",
        "PREV_BRANCH": branch,
        "PREV_COMMIT_SHA": commit_sha,
        "PREV_URL": url,
        "PREV_DOMAIN": domain,
        "PREV_DB_HOST": f"{prefix}-db",
        "PREV_DB_NAME": "drupal",
        "PREV_DB_USER": "drupal",
        "PREV_DB_PASSWORD": "drupal",
        "PREV_FILE_PUBLIC_PATH": "sites/default/files",
        "PREV_FILE_PRIVATE_PATH": "sites/default/files/private",
        "PREV_FILE_TEMP_PATH": "/tmp",
        "PREV_FILE_TRANSLATIONS_PATH": "sites/default/files/translations",
        "DOCUMENT_ROOT": f"/var/www/html/{config['docroot']}",
    }

    if config["services"]["redis"]:
        php_env["PREV_REDIS_HOST"] = f"{prefix}-redis"

    if config["services"]["solr"]:
        php_env["PREV_SOLR_HOST"] = f"{prefix}-solr"
        php_env["PREV_SOLR_CORE"] = "drupal"

    # Merge user env vars from preview.yml
    php_env.update(config["env"])

    # Merge extra env vars (project + preview level from UI)
    if extra_env:
        php_env.update(extra_env)

    # Build compose structure
    # Use prefix as project name to avoid collisions between previews
    # that share the same directory name (e.g. two "branch-main" dirs).
    compose: dict[str, Any] = {
        "name": prefix,
        "services": {
            "php": {
                "image": f"{settings.drupal_base_image}:php{config['php_version']}",
                "container_name": f"{prefix}-php",
                "volumes": ["./:/var/www/html"],
                "environment": php_env,
                "labels": {
                    "caddy": domain,
                    "caddy.reverse_proxy": "{{upstreams 80}}",
                    "caddy.forward_auth": "host.docker.internal:8000",
                    "caddy.forward_auth.uri": "/api/auth/verify-preview",
                    "caddy.forward_auth.header_up": "Host {http.request.host}",
                },
                "networks": [network_name],
                "restart": "unless-stopped",
            },
            "db": {
                "image": db_image,
                "container_name": f"{prefix}-db",
                "environment": {
                    "MYSQL_ROOT_PASSWORD": "root",
                    "MYSQL_DATABASE": "drupal",
                    "MYSQL_USER": "drupal",
                    "MYSQL_PASSWORD": "drupal",
                },
                "volumes": ["db_data:/var/lib/mysql"],
                "networks": [network_name],
                "restart": "unless-stopped",
            },
        },
        "volumes": {
            "db_data": None,
        },
        "networks": {
            network_name: {"external": True},
        },
    }

    # Optional services
    if config["services"]["redis"]:
        compose["services"]["redis"] = {
            "image": "redis:7-alpine",
            "container_name": f"{prefix}-redis",
            "networks": [network_name],
            "restart": "unless-stopped",
        }

    if config["services"]["solr"]:
        compose["services"]["solr"] = {
            "image": "solr:9",
            "container_name": f"{prefix}-solr",
            "volumes": ["solr_data:/var/solr"],
            "command": "solr-precreate drupal",
            "networks": [network_name],
            "restart": "unless-stopped",
        }
        compose["volumes"]["solr_data"] = None

    return compose


def write_docker_compose(preview_path: Path, compose: dict) -> Path:
    """Write the docker-compose.yml to disk."""
    compose_file = preview_path / "docker-compose.yml"
    compose_file.write_text(yaml.dump(compose, default_flow_style=False, sort_keys=False))
    logger.info(f"Generated docker-compose.yml at {compose_file}")
    return compose_file


def detect_docroot(preview_path: Path) -> str:
    """Auto-detect the docroot directory."""
    for candidate in ("web", "docroot"):
        if (preview_path / candidate).is_dir():
            return candidate
    return "web"  # fallback
