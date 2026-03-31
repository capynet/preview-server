"""Seed superadmin user and default organization after a fresh DB reset.

Only runs if the users table is empty. Safe to call on every startup.
"""
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

from config.settings import settings

logger = logging.getLogger(__name__)


def _load_dotenv():
    """Load .env file into os.environ (simple parser, no dependencies)."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def seed_database():
    """Insert seed data if users table is empty."""
    admin_email = _env("SEED_ADMIN_EMAIL")
    if not admin_email:
        print("Seed skipped: SEED_ADMIN_EMAIL not set")
        return

    dsn = settings.database_url.replace("+asyncpg", "")

    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM users")
        count = cur.fetchone()[0]
        if count > 0:
            print(f"Seed skipped: {count} user(s) already exist")
            cur.close()
            conn.close()
            return

        now = datetime.now(timezone.utc).isoformat()

        # 1. Create superadmin
        cur.execute(
            """INSERT INTO users (email, name, is_superadmin, created_at, updated_at)
            VALUES (%s, %s, 1, %s, %s) RETURNING id""",
            (admin_email, _env("SEED_ADMIN_NAME", "Admin"), now, now),
        )
        admin_id = cur.fetchone()[0]

        # 2. Create default organization "Druploy" and make admin the owner
        cur.execute(
            """INSERT INTO organizations (slug, name, auto_erase_enabled, auto_erase_days, color, created_at, updated_at)
            VALUES ('druploy', 'Druploy', 0, 10, '#6366f1', %s, %s) RETURNING id""",
            (now, now),
        )
        org_id = cur.fetchone()[0]

        cur.execute(
            """INSERT INTO org_members (user_id, organization_id, role, created_at)
            VALUES (%s, %s, 'owner', %s)""",
            (admin_id, org_id, now),
        )

        conn.commit()
        print(f"Seed complete: admin '{admin_email}' (id={admin_id}), org 'druploy' (id={org_id})")

    except Exception as e:
        print(f"Seed error: {e}")
        import traceback; traceback.print_exc()
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
