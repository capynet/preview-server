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
        "valkey": False,
    },
    "env": {},
    "deploy": {
        "new": None,
        "update": None,
    },
    "domain_aliases": [],
    "expose": {},
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

    # Database property: "mysql:8.0", "mariadb:10.6", etc.
    SUPPORTED_DB_ENGINES = ("mysql", "mariadb")
    if "database" in raw:
        db_val = str(raw["database"])
        engine = db_val.split(":")[0] if ":" in db_val else db_val
        if engine not in SUPPORTED_DB_ENGINES:
            raise ValueError(f"Unsupported database engine '{engine}'. Supported: {', '.join(SUPPORTED_DB_ENGINES)}")
        config["database"] = db_val
    if "docroot" in raw:
        config["docroot"] = str(raw["docroot"])

    # Services: redis, solr, valkey — defined at root level.
    # false = disabled, version string = enabled with that version.
    for svc in ("redis", "solr", "valkey"):
        if svc in raw:
            val = raw[svc]
            if val is False or val is None:
                config["services"][svc] = False
            elif val is True:
                config["services"][svc] = True
            elif isinstance(val, (str, int, float)):
                config["services"][svc] = str(val)
    # Valkey and redis are mutually exclusive — valkey wins
    if config["services"]["valkey"]:
        config["services"]["redis"] = False

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

    # Domain aliases — additional subdomain prefixes routed to this preview.
    # Each prefix becomes {prefix}--{preview-domain}.mr.preview-mr.com
    if "domain_aliases" in raw and isinstance(raw["domain_aliases"], list):
        config["domain_aliases"] = [str(a) for a in raw["domain_aliases"] if a]

    # Solr configset — path relative to project root with schema.xml etc.
    if "solr_configset" in raw and raw["solr_configset"]:
        config["solr_configset"] = str(raw["solr_configset"])

    # Expose — map service names to ports for subdomain routing via Caddy.
    # e.g. expose: { solr: 8983 } → solr--{preview-domain} routes to container:8983
    if "expose" in raw and isinstance(raw["expose"], dict):
        config["expose"] = {str(k): int(v) for k, v in raw["expose"].items() if v}

    logger.info(f"Parsed preview.yml: php={config['php_version']}, database={config['database']}, "
                f"redis={config['services']['redis']}, valkey={config['services']['valkey']}, "
                f"solr={config['services']['solr']}, "
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

    if config["services"]["redis"] or config["services"]["valkey"]:
        php_env["PREV_REDIS_HOST"] = f"{prefix}-redis"

    if config["services"]["solr"]:
        php_env["PREV_SOLR_HOST"] = f"{prefix}-solr"
        php_env["PREV_SOLR_CORE"] = "drupal"

    # Domain aliases — extra subdomains that route to this preview.
    alias_prefixes = config.get("domain_aliases", [])
    alias_domains = [f"{a}--{domain}" for a in alias_prefixes]
    if alias_domains:
        php_env["PREV_DOMAIN_ALIASES"] = ",".join(alias_domains)

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
                    "caddy": " ".join([domain] + alias_domains),
                    "caddy.reverse_proxy": "{{upstreams 80}}",
                    "caddy.@protected.not.path": (
                        "*.css *.js *.map "
                        "*.png *.jpg *.jpeg *.gif *.svg *.ico *.webp *.avif "
                        "*.woff *.woff2 *.ttf *.eot *.otf "
                        "*.json *.webmanifest *.xml *.txt "
                        "/sites/default/files/*"
                    ),
                    "caddy.forward_auth": "@protected host.docker.internal:8000",
                    "caddy.forward_auth.uri": "/api/auth/verify-preview",
                    "caddy.forward_auth.header_up": "Host {http.request.host}",
                },
                "networks": [network_name],
                "restart": "unless-stopped",
            },
            "db": {
                "image": db_image,
                "container_name": f"{prefix}-db",
                "command": "--innodb-flush-log-at-trx-commit=0",
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
    redis_cfg = config["services"]["redis"]
    valkey_cfg = config["services"]["valkey"]
    solr_cfg = config["services"]["solr"]

    if valkey_cfg:
        valkey_ver = valkey_cfg if isinstance(valkey_cfg, str) else "8"
        compose["services"]["redis"] = {
            "image": f"valkey/valkey:{valkey_ver}-alpine",
            "container_name": f"{prefix}-redis",
            "networks": [network_name],
            "restart": "unless-stopped",
        }
    elif redis_cfg:
        redis_ver = redis_cfg if isinstance(redis_cfg, str) else "7"
        compose["services"]["redis"] = {
            "image": f"redis:{redis_ver}-alpine",
            "container_name": f"{prefix}-redis",
            "networks": [network_name],
            "restart": "unless-stopped",
        }

    if solr_cfg:
        solr_ver = solr_cfg if isinstance(solr_cfg, str) else "9"
        solr_volumes = ["solr_data:/var/solr"]
        solr_service: dict[str, Any] = {
            "image": f"solr:{solr_ver}",
            "container_name": f"{prefix}-solr",
            "networks": [network_name],
            "restart": "unless-stopped",
        }
        # Mount custom configset if specified in preview.yml.
        # We manually create the core directory instead of using solr-precreate
        # because solr-precreate has an elevate.xml conflict bug in Solr 8.
        configset_path = config.get("solr_configset")
        if configset_path:
            solr_volumes.append(f"./{configset_path}:/opt/solr-conf:ro")
            solr_service["entrypoint"] = [
                "bash", "-c",
                "mkdir -p /var/solr/data/drupal/conf /var/solr/data/drupal/data && "
                "cp /opt/solr-conf/* /var/solr/data/drupal/conf/ && "
                "echo name=drupal > /var/solr/data/drupal/core.properties && "
                "chown -R solr:solr /var/solr/data/drupal && "
                "exec solr-foreground",
            ]
        else:
            solr_service["command"] = "solr-precreate drupal"
        solr_service["volumes"] = solr_volumes
        compose["services"]["solr"] = solr_service
        compose["volumes"]["solr_data"] = None

    # Expose — add Caddy labels to services for subdomain routing.
    # Each exposed service gets a {service}--{domain} subdomain with auth.
    for svc_name, port in config.get("expose", {}).items():
        if svc_name not in compose["services"]:
            logger.warning(f"expose: service '{svc_name}' not found in compose, skipping")
            continue
        svc = compose["services"][svc_name]
        expose_domain = f"{svc_name}--{domain}"
        svc.setdefault("labels", {})
        svc["labels"]["caddy"] = expose_domain
        svc["labels"]["caddy.reverse_proxy"] = "{{upstreams " + str(port) + "}}"
        svc["labels"]["caddy.forward_auth"] = "host.docker.internal:8000"
        svc["labels"]["caddy.forward_auth.uri"] = "/api/auth/verify-preview"
        svc["labels"]["caddy.forward_auth.header_up"] = "Host {http.request.host}"

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
