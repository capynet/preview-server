"""Send any transactional email template to a real inbox via Resend.

Uses the production helpers in `app/auth/email.py` so the output is byte-
identical to what real users receive. Requires `RESEND_API_KEY` in the env
(or in the app's `.env` if run from the venv).

See `docs/internal/emails.md` for the template catalogue.

Usage:
    python3 scripts/send_test_email.py --list
    python3 scripts/send_test_email.py magic-link
    python3 scripts/send_test_email.py added-to-org --to me@example.com
    python3 scripts/send_test_email.py project-invitation
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python3 scripts/send_test_email.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_TO = "marcelo.tosco@proton.me"


def _load_helpers():
    """Import production helpers lazily so --list works without the app venv."""
    from app.auth.email import (
        send_added_to_org_email,
        send_added_to_project_email,
        send_magic_link_email,
        send_org_invitation_email,
        send_project_invitation_email,
    )
    from config.settings import settings

    return {
        "magic-link": lambda to: send_magic_link_email(to, SAMPLE["magic_token"]),
        "added-to-org": lambda to: send_added_to_org_email(
            to, SAMPLE["org_name"], SAMPLE["role"]
        ),
        "added-to-project": lambda to: send_added_to_project_email(
            to, SAMPLE["org_name"], SAMPLE["project_name"], SAMPLE["role"]
        ),
        "org-invitation": lambda to: send_org_invitation_email(
            to,
            SAMPLE["org_name"],
            SAMPLE["role"],
            SAMPLE["invite_token"],
            SAMPLE["invited_by_name"],
        ),
        "project-invitation": lambda to: send_project_invitation_email(
            to,
            SAMPLE["org_name"],
            SAMPLE["project_name"],
            SAMPLE["role"],
            SAMPLE["invite_token"],
            SAMPLE["invited_by_name"],
        ),
    }, settings


TEMPLATE_NAMES = (
    "magic-link",
    "added-to-org",
    "added-to-project",
    "org-invitation",
    "project-invitation",
)

# Sample data per template — realistic enough to spot copy issues at a glance.
SAMPLE = {
    "org_name": "Acme Corp",
    "project_name": "soudal",
    "role": "member",
    "invite_token": "demo-invite-token-1234567890abcdef",
    "magic_token": "demo-magic-token-1234567890abcdef",
    "invited_by_name": "Marcelo Tosco",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send a Druploy transactional email template to a test inbox.",
    )
    parser.add_argument(
        "template",
        nargs="?",
        choices=TEMPLATE_NAMES,
        help="Which template to send.",
    )
    parser.add_argument(
        "--to",
        default=DEFAULT_TO,
        help=f"Recipient email address (default: {DEFAULT_TO}).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available templates and exit.",
    )
    args = parser.parse_args()

    if args.list or not args.template:
        print("Available templates:")
        for name in TEMPLATE_NAMES:
            print(f"  - {name}")
        if not args.template:
            print("\nPass one as the first argument, e.g.:")
            print("    python3 scripts/send_test_email.py magic-link")
        return 0

    templates, settings = _load_helpers()

    if not settings.resend_api_key:
        print(
            "ERROR: RESEND_API_KEY is not set. Export it or load the app's .env "
            "(e.g. `set -a && source .env && set +a`).",
            file=sys.stderr,
        )
        return 2

    print(f"Sending '{args.template}' to {args.to} via Resend…")
    templates[args.template](args.to)
    print("Done. Resend errors (if any) are logged via the app logger.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
