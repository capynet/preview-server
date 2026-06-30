"""Shared PostgreSQL database for Druploy — multi-tenant with organizations.

This package was split out of a single ``database.py`` module. Every name that
used to live in ``app.database`` is re-exported here, so existing
``from app.database import X`` imports keep working unchanged.

Submodules:
    _pool         — connection pool + low-level helpers (get_pool, _now, ...)
    organizations — orgs, members, email domains, invitations
    previews      — preview CRUD
    deployments   — deployment CRUD + zombie reaper helpers
    projects      — projects + project members
    cloud         — cloud-resource billing + CI gating
"""

# Pool + shared helpers (incl. the underscore helpers some modules import directly)
from app.database._pool import (  # noqa: F401
    init_pool,
    close_pool,
    get_pool,
    compute_url_hash,
    _now,
    _row_to_dict,
)

# Entity CRUD — star-import re-exports every public function unchanged.
from app.database.organizations import *  # noqa: F401,F403
from app.database.previews import *  # noqa: F401,F403
from app.database.deployments import *  # noqa: F401,F403
from app.database.projects import *  # noqa: F401,F403
from app.database.cloud import *  # noqa: F401,F403
