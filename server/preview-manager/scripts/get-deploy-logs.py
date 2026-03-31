"""Fetch deployment logs from PostgreSQL.

Usage (from local via SSH):
    cat scripts/get-deploy-logs.py | ssh root@91.99.157.66 \
        "cd /home/druploy/www/previews/server/preview-manager && source venv/bin/activate && python3 - mr-1593"

    # Or with deployment ID:
    cat scripts/get-deploy-logs.py | ssh root@91.99.157.66 \
        "cd /home/druploy/www/previews/server/preview-manager && source venv/bin/activate && python3 - --id 156"

    # Last N deployments for a preview:
    cat scripts/get-deploy-logs.py | ssh root@91.99.157.66 \
        "cd /home/druploy/www/previews/server/preview-manager && source venv/bin/activate && python3 - mr-1593 --last 3"
"""

import argparse
import asyncio
import sys

import asyncpg


async def main():
    parser = argparse.ArgumentParser(description="Fetch deployment logs")
    parser.add_argument("preview", nargs="?", help="Preview name (e.g. mr-1593)")
    parser.add_argument("--id", type=int, help="Deployment ID")
    parser.add_argument("--last", type=int, default=1, help="Show last N deployments (default: 1)")
    parser.add_argument("--status", action="store_true", help="Show only status, no logs")
    args = parser.parse_args()

    if not args.preview and not args.id:
        parser.error("Provide a preview name or --id")

    # Read DB URL from .env
    db_url = None
    try:
        with open(".env") as f:
            for line in f:
                if line.startswith("DATABASE_URL="):
                    db_url = line.strip().split("=", 1)[1]
                    break
    except FileNotFoundError:
        pass
    if not db_url:
        db_url = "postgresql://preview_manager:preview_manager@localhost:5432/preview_manager"

    conn = await asyncpg.connect(db_url)

    try:
        if args.id:
            rows = await conn.fetch(
                """SELECT d.id, d.status, d.error, d.log_output, d.triggered_by,
                          d.started_at, d.completed_at, d.duration, d.type,
                          p.preview_name, pr.slug AS project_slug
                   FROM deployments d
                   JOIN previews p ON d.preview_id = p.id
                   JOIN projects pr ON p.project_id = pr.id
                   WHERE d.id = $1""",
                args.id,
            )
        else:
            rows = await conn.fetch(
                """SELECT d.id, d.status, d.error, d.log_output, d.triggered_by,
                          d.started_at, d.completed_at, d.duration, d.type,
                          p.preview_name, pr.slug AS project_slug
                   FROM deployments d
                   JOIN previews p ON d.preview_id = p.id
                   JOIN projects pr ON p.project_id = pr.id
                   WHERE p.preview_name = $1
                   ORDER BY d.id DESC
                   LIMIT $2""",
                args.preview,
                args.last,
            )

        if not rows:
            print("No deployments found.")
            return

        for row in reversed(rows):
            print(f"{'='*60}")
            print(f"Deploy #{row['id']}  |  {row['project_slug']}/{row['preview_name']}")
            print(f"Status: {row['status']}  |  Type: {row['type']}  |  Triggered by: {row['triggered_by']}")
            print(f"Started: {row['started_at']}  |  Completed: {row['completed_at'] or '-'}")
            if row["duration"]:
                print(f"Duration: {row['duration']}s")
            if row["error"]:
                print(f"Error: {row['error']}")
            print(f"{'='*60}")

            if not args.status:
                log = row["log_output"] or ""
                if len(log) <= 2:
                    print("(no logs)")
                else:
                    print(log)
            print()
    finally:
        await conn.close()


asyncio.run(main())
