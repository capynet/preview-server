"""Cron jobs validation and cascading helpers.

Cron jobs are stored as a JSON array of objects with this shape:

    {
        "name": "drupal-cron",
        "schedule": "*/15 * * * *",
        "command": "drush cron",
        "enabled": true
    }

The minimum interval is 1 minute (standard 5-field cron). Project-level
jobs cascade into previews: a preview job with the same `name` as a project
job fully overrides it.
"""

import json
import re
from typing import Any

CRON_FIELD_RE = re.compile(r"^[\d*/,\-]+$")
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class CronJobValidationError(ValueError):
    pass


def _validate_schedule(schedule: str) -> None:
    """Lightweight validation of a 5-field cron expression.

    We don't evaluate ranges/values — supercronic will reject invalid schedules
    at load time. We only ensure the shape is "5 whitespace-separated fields
    with allowed characters" so obvious typos are caught early.
    """
    if not isinstance(schedule, str) or not schedule.strip():
        raise CronJobValidationError("schedule must be a non-empty string")
    parts = schedule.strip().split()
    if len(parts) != 5:
        raise CronJobValidationError(
            f"schedule must have 5 fields (min hour day month dow), got {len(parts)}"
        )
    for p in parts:
        if not CRON_FIELD_RE.match(p):
            raise CronJobValidationError(f"invalid schedule field: {p!r}")


def validate_cron_jobs(raw: Any) -> list[dict]:
    """Validate and normalize a list of cron jobs.

    Returns a list of clean dicts with keys: name, schedule, command, enabled.
    Raises CronJobValidationError on bad input.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CronJobValidationError("cron_jobs must be a list")

    cleaned: list[dict] = []
    seen_names: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CronJobValidationError(f"cron_jobs[{i}] must be an object")

        name = item.get("name", "").strip() if isinstance(item.get("name"), str) else ""
        if not name:
            raise CronJobValidationError(f"cron_jobs[{i}].name is required")
        if not NAME_RE.match(name):
            raise CronJobValidationError(
                f"cron_jobs[{i}].name must match [a-zA-Z0-9_-]+"
            )
        if name in seen_names:
            raise CronJobValidationError(f"duplicate cron_jobs name: {name!r}")
        seen_names.add(name)

        schedule = item.get("schedule", "")
        _validate_schedule(schedule)

        command = item.get("command", "")
        if not isinstance(command, str) or not command.strip():
            raise CronJobValidationError(f"cron_jobs[{i}].command is required")

        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise CronJobValidationError(f"cron_jobs[{i}].enabled must be a boolean")

        cleaned.append({
            "name": name,
            "schedule": schedule.strip(),
            "command": command.strip(),
            "enabled": enabled,
        })
    return cleaned


def load_cron_jobs(raw: Any) -> list[dict]:
    """Parse a cron_jobs value coming from DB (TEXT JSON, list, or None)."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    else:
        parsed = raw
    if not isinstance(parsed, list):
        return []
    return [j for j in parsed if isinstance(j, dict)]


def merge_cron_jobs(project_jobs: list[dict], preview_jobs: list[dict]) -> list[dict]:
    """Cascade project jobs with preview overrides.

    A preview job with the same `name` as a project job fully replaces it.
    Disabled jobs (enabled=False) are dropped from the result — they should
    not reach the VM agent.
    """
    merged: dict[str, dict] = {}
    for j in project_jobs:
        name = j.get("name")
        if name:
            merged[name] = j
    for j in preview_jobs:
        name = j.get("name")
        if name:
            merged[name] = j
    return [j for j in merged.values() if j.get("enabled", True)]
