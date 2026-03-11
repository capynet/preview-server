"""Seed default users, organization, and project after a fresh DB reset.

Only runs if the users table is empty. Safe to call on every startup.
"""
import logging
from datetime import datetime, timezone

import psycopg2

from config.settings import settings

logger = logging.getLogger(__name__)

# Admin superuser (password: admin)
ADMIN_EMAIL = "capy.net@gmail.com"
ADMIN_NAME = "Admin"
ADMIN_PASSWORD_HASH = "$2b$12$WmkZhZBZyyCZtlfCCiGoZ.YLHaNcFV1QcVZ0qxYspcUCeTegeij5S"

# Test user (logs in via Google OAuth only)
TEST_EMAIL = "marcelo.tosco@dropsolid.com"
TEST_NAME = "Marcelo Tosco"
TEST_GOOGLE_PROVIDER_ID = "113394955263071304752"

# Organization
ORG_SLUG = "dropsolid"
ORG_NAME = "Dropsolid"
ORG_COLOR = "#fb923c"
ORG_GITLAB_URL = "https://gitlab.dropsolid.com"
ORG_GITLAB_TOKEN = "glpat-K533KIaQFHCXEVEGghGgmG86MQp1OjZhNQk.01.0z060rsh9"

# Project
PROJECT_SLUG = "soudal"
PROJECT_NAME = "soudal"
PROJECT_GITLAB_ID = 461
PROJECT_GITLAB_PATH = "project/soudal"
PROJECT_GITLAB_URL = "https://gitlab.dropsolid.com/project/soudal"
PROJECT_GITLAB_BRANCH = "master"
PROJECT_ENV_VARS = '{"COMPOSER_AUTH": "{\\"gitlab-token\\":{\\"gitlab.internal.dropsolid.com\\":\\"aGkS9VirFboZA4gCCk5EBW86MQp1Ojc2CA.01.0y10jg2nu\\"}}"}'

# Email domain auto-join
EMAIL_DOMAIN = "dropsolid.com"
EMAIL_DOMAIN_ROLE = "member"


def seed_database():
    """Insert seed data if users table is empty."""
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
            """INSERT INTO users (email, name, password_hash, is_superadmin, created_at, updated_at)
            VALUES (%s, %s, %s, 1, %s, %s) RETURNING id""",
            (ADMIN_EMAIL, ADMIN_NAME, ADMIN_PASSWORD_HASH, now, now),
        )
        admin_id = cur.fetchone()[0]

        # 2. Create test user (no password — uses Google OAuth)
        cur.execute(
            """INSERT INTO users (email, name, is_superadmin, created_at, updated_at)
            VALUES (%s, %s, 0, %s, %s) RETURNING id""",
            (TEST_EMAIL, TEST_NAME, now, now),
        )
        test_user_id = cur.fetchone()[0]

        # 3. Google OAuth account for test user
        cur.execute(
            """INSERT INTO oauth_accounts (user_id, provider, provider_user_id, provider_username, created_at)
            VALUES (%s, 'google', %s, %s, %s)""",
            (test_user_id, TEST_GOOGLE_PROVIDER_ID, TEST_EMAIL, now),
        )

        # 4. Organization
        cur.execute(
            """INSERT INTO organizations (slug, name, avatar_url, gitlab_url, gitlab_access_token,
            auto_erase_enabled, auto_erase_days, color, created_at, updated_at)
            VALUES (%s, %s, NULL, %s, %s, 0, 10, %s, %s, %s) RETURNING id""",
            (ORG_SLUG, ORG_NAME, ORG_GITLAB_URL, ORG_GITLAB_TOKEN, ORG_COLOR, now, now),
        )
        org_id = cur.fetchone()[0]

        # 5. Test user is owner of the org
        cur.execute(
            """INSERT INTO org_members (user_id, organization_id, role, created_at)
            VALUES (%s, %s, 'owner', %s)""",
            (test_user_id, org_id, now),
        )

        # 6. Email domain auto-join
        cur.execute(
            """INSERT INTO org_email_domains (organization_id, domain, default_role, created_at)
            VALUES (%s, %s, %s, %s)""",
            (org_id, EMAIL_DOMAIN, EMAIL_DOMAIN_ROLE, now),
        )

        # 7. Project
        cur.execute(
            """INSERT INTO projects (organization_id, slug, name, gitlab_project_id, gitlab_project_path,
            gitlab_web_url, gitlab_default_branch, env_vars, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (org_id, PROJECT_SLUG, PROJECT_NAME, PROJECT_GITLAB_ID, PROJECT_GITLAB_PATH,
             PROJECT_GITLAB_URL, PROJECT_GITLAB_BRANCH, PROJECT_ENV_VARS, now, now),
        )

        conn.commit()
        print(f"Seed complete: admin (id={admin_id}), test user (id={test_user_id}), org '{ORG_SLUG}', project '{PROJECT_SLUG}'")

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
