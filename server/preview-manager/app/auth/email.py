"""Email sending via Resend for membership notifications and sign-in.

See `docs/internal/emails.md` for the full catalogue of emails sent by the
backend, triggers, payloads, and how to test each template locally.
"""

import logging
from urllib.parse import quote

import resend

from config.settings import settings

logger = logging.getLogger(__name__)


ROLE_LABELS = {
    "owner": "Owner",
    "admin": "Admin",
    "member": "Member",
    "viewer": "Viewer",
}


def _role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role.title())


def _layout(heading: str, intro_html: str, cta_label: str, cta_url: str, footer_html: str = "") -> str:
    """Shared HTML shell for every transactional email."""
    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 520px; margin: 0 auto; padding: 40px 20px;">
      <h2 style="margin: 0 0 16px; font-size: 20px; color: #111;">{heading}</h2>
      <div style="margin: 0 0 24px; color: #555; font-size: 14px; line-height: 1.5;">{intro_html}</div>
      <a href="{cta_url}"
         style="display: inline-block; background: #111; color: #fff; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 500;">
        {cta_label}
      </a>
      {f'<div style="margin: 24px 0 0; color: #999; font-size: 12px; line-height: 1.5;">{footer_html}</div>' if footer_html else ''}
    </div>
    """


def _send(to_email: str, subject: str, html: str, log_label: str) -> None:
    """Send through Resend, swallowing failures with a log entry."""
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not configured, skipping %s email to %s", log_label, to_email)
        return

    resend.api_key = settings.resend_api_key
    try:
        resend.Emails.send({
            "from": settings.invitation_from_email,
            "to": [to_email],
            "subject": subject,
            "html": html,
        })
        logger.info("%s email sent to %s", log_label, to_email)
    except Exception as e:
        logger.error("Failed to send %s email to %s: %s", log_label, to_email, e)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_SIGN_IN_OPTIONS = (
    "You can sign in with Google, GitLab, or a magic link sent to your inbox."
)


def _login_url(email: str) -> str:
    """Login URL with email pre-filled so the magic-link input is ready to use."""
    return f"{settings.frontend_url}/auth/login?email={quote(email, safe='@')}"


def send_magic_link_email(to_email: str, token: str) -> None:
    """One-click sign-in link. Token is consumed on first click and expires in 15 min."""
    magic_url = f"{settings.api_url}/api/auth/magic/verify?token={token}"
    html = _layout(
        heading="Sign in to Druploy",
        intro_html="Click the button below to sign in. This link expires in 15 minutes.",
        cta_label="Sign in",
        cta_url=magic_url,
        footer_html="If you didn't request this, you can safely ignore this email.",
    )
    _send(to_email, "Sign in to Druploy", html, "magic-link")


def send_added_to_org_email(to_email: str, org_name: str, role: str) -> None:
    """Notify an existing user that they were added directly to an organization."""
    role_label = _role_label(role)
    subject = f"You've been added to {org_name} on Druploy"
    intro = (
        f"You now have access to the <strong>{org_name}</strong> organization "
        f"as <strong>{role_label}</strong>. {_SIGN_IN_OPTIONS}"
    )
    html = _layout(
        heading=f"Welcome to {org_name}",
        intro_html=intro,
        cta_label="Sign in to Druploy",
        cta_url=_login_url(to_email),
    )
    _send(to_email, subject, html, "added-to-org")


def send_added_to_project_email(to_email: str, org_name: str, project_name: str, role: str) -> None:
    """Notify an existing user that they were added to a specific project."""
    role_label = _role_label(role)
    subject = f"You've been added to {project_name} ({org_name}) on Druploy"
    intro = (
        f"You now have access to the <strong>{project_name}</strong> project "
        f"in the <strong>{org_name}</strong> organization as "
        f"<strong>{role_label}</strong>. {_SIGN_IN_OPTIONS}"
    )
    html = _layout(
        heading=f"Welcome to {project_name}",
        intro_html=intro,
        cta_label="Sign in to Druploy",
        cta_url=_login_url(to_email),
    )
    _send(to_email, subject, html, "added-to-project")


def send_org_invitation_email(
    to_email: str,
    org_name: str,
    role: str,
    invite_token: str,
    invited_by_name: str | None = None,
) -> None:
    """Invite a new (not yet registered) user to an organization."""
    role_label = _role_label(role)
    accept_url = f"{settings.frontend_url}/auth/invite?token={invite_token}"
    by = f" by {invited_by_name}" if invited_by_name else ""
    subject = f"You're invited to {org_name} on Druploy"
    intro = (
        f"You've been invited{by} to join the <strong>{org_name}</strong> "
        f"organization as <strong>{role_label}</strong>. Accept the invitation "
        f"to create your account."
    )
    html = _layout(
        heading=f"Join {org_name} on Druploy",
        intro_html=intro,
        cta_label="Accept invitation",
        cta_url=accept_url,
        footer_html="If you weren't expecting this invitation, you can ignore this email.",
    )
    _send(to_email, subject, html, "org-invitation")


def send_project_invitation_email(
    to_email: str,
    org_name: str,
    project_name: str,
    role: str,
    invite_token: str,
    invited_by_name: str | None = None,
) -> None:
    """Invite a new (not yet registered) user to a specific project."""
    role_label = _role_label(role)
    accept_url = f"{settings.frontend_url}/auth/invite?token={invite_token}"
    by = f" by {invited_by_name}" if invited_by_name else ""
    subject = f"You're invited to {project_name} ({org_name}) on Druploy"
    intro = (
        f"You've been invited{by} to join the <strong>{project_name}</strong> "
        f"project in the <strong>{org_name}</strong> organization as "
        f"<strong>{role_label}</strong>. Accept the invitation to create your "
        f"account."
    )
    html = _layout(
        heading=f"Join {project_name} on Druploy",
        intro_html=intro,
        cta_label="Accept invitation",
        cta_url=accept_url,
        footer_html="If you weren't expecting this invitation, you can ignore this email.",
    )
    _send(to_email, subject, html, "project-invitation")
