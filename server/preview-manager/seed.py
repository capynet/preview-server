"""Seed default users, organization, and project after a fresh DB reset.

Only runs if the users table is empty. Safe to call on every startup.
All sensitive values are read from environment variables (set via Ansible vault).
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
    # Check that required seed vars are present
    admin_email = _env("SEED_ADMIN_EMAIL")
    if not admin_email:
        print("Seed skipped: SEED_ADMIN_EMAIL not set")
        return

    dsn = settings.database_url
    # psycopg2 uses postgresql:// not postgresql+asyncpg://
    dsn = dsn.replace("+asyncpg", "")

    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        cur = conn.cursor()

        # Check if there are already users
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
            (_env("SEED_ADMIN_EMAIL"), _env("SEED_ADMIN_NAME"), now, now),
        )
        admin_id = cur.fetchone()[0]

        # 2. Create test user (no password — uses Google OAuth)
        test_email = _env("SEED_TEST_EMAIL")
        test_user_id = None
        if test_email:
            cur.execute(
                """INSERT INTO users (email, name, is_superadmin, created_at, updated_at)
                VALUES (%s, %s, 0, %s, %s) RETURNING id""",
                (test_email, _env("SEED_TEST_NAME"), now, now),
            )
            test_user_id = cur.fetchone()[0]

            # 3. Google OAuth account for test user
            google_id = _env("SEED_TEST_GOOGLE_ID")
            if google_id:
                cur.execute(
                    """INSERT INTO oauth_accounts (user_id, provider, provider_user_id, provider_username, created_at)
                    VALUES (%s, 'google', %s, %s, %s)""",
                    (test_user_id, google_id, test_email, now),
                )

        # 4. Organization
        org_slug = _env("SEED_ORG_SLUG")
        org_id = None
        if org_slug:
            cur.execute(
                """INSERT INTO organizations (slug, name, avatar_url, gitlab_url, gitlab_access_token,
                auto_erase_enabled, auto_erase_days, color, created_at, updated_at)
                VALUES (%s, %s, NULL, %s, %s, 0, 10, %s, %s, %s) RETURNING id""",
                (org_slug, _env("SEED_ORG_NAME"), _env("SEED_ORG_GITLAB_URL"),
                 _env("SEED_ORG_GITLAB_TOKEN"), _env("SEED_ORG_COLOR"), now, now),
            )
            org_id = cur.fetchone()[0]

            # 5. Test user is owner of the org
            if test_user_id:
                cur.execute(
                    """INSERT INTO org_members (user_id, organization_id, role, created_at)
                    VALUES (%s, %s, 'owner', %s)""",
                    (test_user_id, org_id, now),
                )

            # 6. Email domain auto-join
            email_domain = _env("SEED_EMAIL_DOMAIN")
            if email_domain:
                cur.execute(
                    """INSERT INTO org_email_domains (organization_id, domain, default_role, created_at)
                    VALUES (%s, %s, %s, %s)""",
                    (org_id, email_domain, _env("SEED_EMAIL_DOMAIN_ROLE", "member"), now),
                )

            # 7. Project
            project_slug = _env("SEED_PROJECT_SLUG")
            if project_slug:
                cur.execute(
                    """INSERT INTO projects (organization_id, slug, name, gitlab_project_id, gitlab_project_path,
                    gitlab_web_url, gitlab_default_branch, env_vars, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (org_id, project_slug, _env("SEED_PROJECT_NAME"), _env("SEED_PROJECT_GITLAB_ID"),
                     _env("SEED_PROJECT_GITLAB_PATH"), _env("SEED_PROJECT_GITLAB_URL"),
                     _env("SEED_PROJECT_GITLAB_BRANCH"), _env("SEED_PROJECT_ENV_VARS"), now, now),
                )

        conn.commit()
        print(f"Seed complete: admin (id={admin_id}), test user (id={test_user_id}), org '{org_slug}', project '{_env('SEED_PROJECT_SLUG')}'")

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
