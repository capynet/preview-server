"""Email sending via Resend for membership notifications."""

import logging

import resend

from config.settings import settings

logger = logging.getLogger(__name__)

APP_URL = "https://app.preview-mr.com"


def _build_added_email(entity_type: str, entity_name: str, role: str) -> tuple[str, str]:
    """Build subject and HTML for an 'added to org/project' email."""
    subject = f"You've been added to {entity_name}"

    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 480px; margin: 0 auto; padding: 40px 20px;">
      <h2 style="margin: 0 0 16px; font-size: 20px; color: #111;">
        You've been added to <strong>{entity_name}</strong> {entity_type} as <strong>{role}</strong>
      </h2>
      <p style="margin: 0 0 24px; color: #666; font-size: 14px;">
        Access {APP_URL} and sign in with your Google or GitLab account.
      </p>
      <a href="{APP_URL}"
         style="display: inline-block; background: #111; color: #fff; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 500;">
        Let's go there!
      </a>
    </div>
    """
    return subject, html


def send_added_to_org_email(to_email: str, org_name: str, role: str):
    """Send notification when user is added to an organization."""
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not configured, skipping email send")
        return

    resend.api_key = settings.resend_api_key
    subject, html = _build_added_email("organization", org_name, role)

    try:
        resend.Emails.send({
            "from": settings.invitation_from_email,
            "to": [to_email],
            "subject": subject,
            "html": html,
        })
        logger.info(f"Added-to-org email sent to {to_email}")
    except Exception as e:
        logger.error(f"Failed to send added-to-org email to {to_email}: {e}")


def send_added_to_project_email(to_email: str, project_name: str, role: str):
    """Send notification when user is added to a project."""
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not configured, skipping email send")
        return

    resend.api_key = settings.resend_api_key
    subject, html = _build_added_email("project", project_name, role)

    try:
        resend.Emails.send({
            "from": settings.invitation_from_email,
            "to": [to_email],
            "subject": subject,
            "html": html,
        })
        logger.info(f"Added-to-project email sent to {to_email}")
    except Exception as e:
        logger.error(f"Failed to send added-to-project email to {to_email}: {e}")


def send_magic_link_email(to_email: str, token: str):
    """Send a magic link sign-in email."""
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not configured, skipping magic link email")
        return

    resend.api_key = settings.resend_api_key
    magic_url = f"{APP_URL}/auth/magic?token={token}"

    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 480px; margin: 0 auto; padding: 40px 20px;">
      <h2 style="margin: 0 0 16px; font-size: 20px; color: #111;">
        Sign in to Preview Manager
      </h2>
      <p style="margin: 0 0 24px; color: #666; font-size: 14px;">
        Click the button below to sign in. This link expires in 15 minutes.
      </p>
      <a href="{magic_url}"
         style="display: inline-block; background: #111; color: #fff; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 500;">
        Sign in
      </a>
      <p style="margin: 24px 0 0; color: #999; font-size: 12px;">
        If you didn't request this, you can safely ignore this email.
      </p>
    </div>
    """

    try:
        resend.Emails.send({
            "from": settings.invitation_from_email,
            "to": [to_email],
            "subject": "Sign in to Preview Manager",
            "html": html,
        })
        logger.info(f"Magic link email sent to {to_email}")
    except Exception as e:
        logger.error(f"Failed to send magic link email to {to_email}: {e}")


# Keep backward compat alias for invitation emails (same as org add)
def send_invitation_email(to_email: str, invite_token: str, role: str, invited_by_name: str):
    """Legacy invitation email — now sends the same 'added' email."""
    send_added_to_org_email(to_email, "Preview Manager", role)
