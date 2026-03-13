#!/usr/bin/env python3
"""
Load test: enqueue rebuild (update) jobs for all active previews via arq.

Usage (run on the server):
    cd /home/preview-manager/www/previews/server/preview-manager
    source venv/bin/activate
    python3 scripts/load-test-rebuild.py

Or from local via SSH:
    cat preview-server/server/preview-manager/scripts/load-test-rebuild.py | \
        ssh root@91.99.157.66 "cd /home/preview-manager/www/previews/server/preview-manager && source venv/bin/activate && python3"
"""

import asyncio
import asyncpg
from arq import create_pool
from arq.connections import RedisSettings


POSTGRES_DSN = "postgresql://preview_manager:preview_manager@localhost:5432/preview_manager"
VALKEY_HOST = "localhost"
VALKEY_PORT = 6379


async def main():
    # Fetch all active previews from DB
    conn = await asyncpg.connect(POSTGRES_DSN)
    rows = await conn.fetch("""
        SELECT
            o.id AS org_id, o.slug AS org_slug,
            p.id AS project_id, p.slug AS project_slug, p.gitlab_project_path,
            pr.preview_name, pr.branch, pr.commit_sha, pr.mr_id
        FROM previews pr
        JOIN projects p ON p.id = pr.project_id
        JOIN organizations o ON o.id = p.organization_id
    """)
    await conn.close()

    if not rows:
        print("No active previews found.")
        return

    print(f"Found {len(rows)} previews. Enqueuing rebuild jobs...")

    pool = await create_pool(RedisSettings(host=VALKEY_HOST, port=VALKEY_PORT))
    for r in rows:
        job = await pool.enqueue_job(
            "task_deploy_preview",
            r["org_id"],
            r["org_slug"],
            r["project_id"],
            r["project_slug"],
            r["gitlab_project_path"],
            r["preview_name"],
            r["branch"],
            r["commit_sha"],
            "update",      # triggered_by
            r["mr_id"],    # mr_iid
            False,         # force_new
        )
        print(f"  Enqueued: {r['project_slug']}/{r['preview_name']} -> {job.job_id}")

    await pool.aclose()
    print(f"\nDone. {len(rows)} jobs enqueued.")


if __name__ == "__main__":
    asyncio.run(main())
