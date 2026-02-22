"""Email sending via Resend for user invitations."""

import logging

import resend

from config.settings import settings

logger = logging.getLogger(__name__)


def send_invitation_email(to_email: str, invite_token: str, role: str, invited_by_name: str):
    """Send an invitation email via Resend."""
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not configured, skipping email send")
        return

    resend.api_key = settings.resend_api_key
    accept_url = f"{settings.frontend_url}/auth/invite?token={invite_token}"

    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 480px; margin: 0 auto; padding: 40px 20px;">
      <h2 style="margin: 0 0 8px; font-size: 20px; color: #111;">You've been invited to Preview Manager</h2>
      <p style="margin: 0 0 16px; color: #666; font-size: 14px;">
        {invited_by_name} has invited you to join as <strong>{role}</strong>.
      </p>
      <p style="margin: 0 0 24px; color: #666; font-size: 14px;">
        You can set a password using the button below, or simply go to
        <a href="{settings.frontend_url}" style="color: #111; text-decoration: underline;">{settings.frontend_url}</a>
        and sign in with your Google or GitLab account.
      </p>
      <a href="{accept_url}"
         style="display: inline-block; background: #111; color: #fff; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 500;">
        Set password
      </a>
      <p style="margin: 24px 0 0; color: #999; font-size: 12px;">
        This invitation expires in 7 days. If you didn't expect this, you can ignore this email.
      </p>
    </div>
    """

    try:
        resend.Emails.send({
            "from": settings.invitation_from_email,
            "to": [to_email],
            "subject": f"{invited_by_name} invited you to Preview Manager",
            "html": html,
        })
        logger.info(f"Invitation email sent to {to_email}")
    except Exception as e:
        logger.error(f"Failed to send invitation email to {to_email}: {e}")
        raise
