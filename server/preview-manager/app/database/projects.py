"""Project + project-member CRUD."""

from typing import Optional

from app.database._pool import get_pool, _now, _row_to_dict


async def get_project(project_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
    return _row_to_dict(row) if row else None


async def get_project_by_slug(org_id: int, slug: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM projects WHERE organization_id = $1 AND slug = $2",
        org_id, slug,
    )
    return _row_to_dict(row) if row else None


async def resolve_project_by_slug(user_id: int, slug: str) -> list[dict]:
    """Find projects matching a slug across all orgs the user has access to.

    Returns list of dicts with project + org info (org_slug, org_name).
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT p.*, o.slug as org_slug, o.name as org_name
           FROM projects p
           JOIN organizations o ON o.id = p.organization_id
           WHERE p.slug = $2
             AND (
               EXISTS (SELECT 1 FROM org_members om WHERE om.organization_id = o.id AND om.user_id = $1)
               OR EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id = p.id AND pm.user_id = $1)
             )
           ORDER BY o.name""",
        user_id, slug,
    )
    return [_row_to_dict(r) for r in rows]


async def get_project_by_gitlab_id(org_id: int, gitlab_project_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM projects WHERE organization_id = $1 AND gitlab_project_id = $2",
        org_id, gitlab_project_id,
    )
    return _row_to_dict(row) if row else None


async def list_projects(org_id: int) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM projects WHERE organization_id = $1 ORDER BY slug",
        org_id,
    )
    return [_row_to_dict(r) for r in rows]


async def list_user_projects(org_id: int, user_id: int) -> list[dict]:
    """List projects the user has access to via project_members."""
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT p.* FROM projects p
           JOIN project_members pm ON pm.project_id = p.id
           WHERE p.organization_id = $1 AND pm.user_id = $2
           ORDER BY p.slug""",
        org_id, user_id,
    )
    return [_row_to_dict(r) for r in rows]


async def upsert_project(org_id: int, slug: str, **fields) -> dict:
    pool = await get_pool()
    existing = await pool.fetchrow(
        "SELECT * FROM projects WHERE organization_id = $1 AND slug = $2",
        org_id, slug,
    )
    now = _now()

    if existing:
        sets = ["updated_at = $1"]
        vals = [now]
        idx = 2
        for k, v in fields.items():
            sets.append(f"{k} = ${idx}")
            vals.append(v)
            idx += 1
        vals.append(existing["id"])
        await pool.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ${idx}", *vals
        )
    else:
        await pool.execute(
            """INSERT INTO projects
               (organization_id, slug, name, gitlab_project_id, gitlab_project_path,
                gitlab_web_url, gitlab_default_branch, env_vars, cron_jobs,
                skip_source_branches, skip_target_branches,
                created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
            org_id, slug,
            fields.get("name"),
            fields.get("gitlab_project_id"),
            fields.get("gitlab_project_path"),
            fields.get("gitlab_web_url"),
            fields.get("gitlab_default_branch", "main"),
            fields.get("env_vars", "{}"),
            fields.get("cron_jobs", "[]"),
            fields.get("skip_source_branches", "[]"),
            fields.get("skip_target_branches", "[]"),
            now, now,
        )
    row = await pool.fetchrow(
        "SELECT * FROM projects WHERE organization_id = $1 AND slug = $2",
        org_id, slug,
    )
    return _row_to_dict(row)


async def add_project_member(user_id: int, project_id: int, added_by: int, role: str = "viewer"):
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO project_members (user_id, project_id, added_by, role, created_at)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT(user_id, project_id) DO UPDATE SET role = EXCLUDED.role""",
        user_id, project_id, added_by, role, _now(),
    )


async def get_project_member(user_id: int, project_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM project_members WHERE user_id = $1 AND project_id = $2",
        user_id, project_id,
    )
    return _row_to_dict(row) if row else None


async def list_project_members(project_id: int) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT u.id, u.email, u.name, u.avatar_url, pm.role, pm.created_at
           FROM project_members pm
           JOIN users u ON pm.user_id = u.id
           WHERE pm.project_id = $1
           ORDER BY u.name""",
        project_id,
    )
    return [_row_to_dict(r) for r in rows]


async def update_project_member_role(user_id: int, project_id: int, role: str):
    pool = await get_pool()
    await pool.execute(
        "UPDATE project_members SET role = $1 WHERE user_id = $2 AND project_id = $3",
        role, user_id, project_id,
    )


async def remove_project_member(user_id: int, project_id: int):
    pool = await get_pool()
    await pool.execute(
        "DELETE FROM project_members WHERE user_id = $1 AND project_id = $2",
        user_id, project_id,
    )


async def delete_project(project_id: int):
    pool = await get_pool()
    await pool.execute("DELETE FROM projects WHERE id = $1", project_id)
