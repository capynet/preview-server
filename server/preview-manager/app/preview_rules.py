"""Project-level rules for auto-creating previews from GitLab MR webhooks.

Two independent lists of glob patterns (fnmatch syntax) live on the project:

- ``skip_source_branches``: patterns matched against the MR source branch.
- ``skip_target_branches``: patterns matched against the MR target branch.

Draft MRs are always skipped regardless of patterns. The rules only block
the **initial creation** of a preview by the webhook: if a preview already
exists (e.g. the user created it manually from the UI), subsequent update
events are processed normally.
"""

import fnmatch
import json
from typing import Any


class PreviewRulesValidationError(ValueError):
    pass


def _validate_pattern_list(raw: Any, field: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PreviewRulesValidationError(f"{field} must be a list")
    cleaned: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise PreviewRulesValidationError(f"{field}[{i}] must be a string")
        p = item.strip()
        if not p:
            continue
        if p in cleaned:
            continue  # silently dedupe
        cleaned.append(p)
    return cleaned


def validate_preview_rules(raw: Any) -> dict[str, list[str]]:
    """Validate and normalize an incoming preview-rules payload.

    Accepts ``{"skip_source_branches": [...], "skip_target_branches": [...]}``.
    Missing keys default to empty lists.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PreviewRulesValidationError("preview_rules must be an object")
    return {
        "skip_source_branches": _validate_pattern_list(
            raw.get("skip_source_branches"), "skip_source_branches"
        ),
        "skip_target_branches": _validate_pattern_list(
            raw.get("skip_target_branches"), "skip_target_branches"
        ),
    }


def load_patterns(raw: Any) -> list[str]:
    """Parse a DB-stored pattern list (TEXT JSON, list, or None)."""
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
    return [p for p in parsed if isinstance(p, str) and p.strip()]


def matches_any(value: str, patterns: list[str]) -> str | None:
    """Return the first pattern that matches ``value`` (fnmatch), or None."""
    if not value:
        return None
    for p in patterns:
        if fnmatch.fnmatchcase(value, p):
            return p
    return None


def is_protected_from_auto_delete(preview: dict | None) -> bool:
    """Return True if a preview must NOT be deleted by any automatic path.

    A ``pinned`` preview ("prevent auto erase" in the UI) is protected from
    *every* automatic deletion path — the idle auto-erase cron AND the MR
    close/merge webhook. Manual deletes from the UI (and org/project cascade
    deletes) deliberately bypass this guard.

    This is the single source of truth for the rule; all automatic deletion
    paths must consult it rather than re-checking ``pinned`` themselves.
    """
    return bool(preview and preview.get("pinned"))


def should_skip_auto_creation(
    project: dict,
    source_branch: str,
    target_branch: str | None,
    is_draft: bool,
) -> str | None:
    """Decide whether to skip auto-creation of a preview for a webhook MR.

    Returns a human-readable reason string if the preview should be skipped,
    or ``None`` to proceed. Caller must already have verified that the
    preview does not currently exist — these rules never block updates to
    existing previews.
    """
    if is_draft:
        return "MR is draft"

    source_patterns = load_patterns(project.get("skip_source_branches"))
    matched = matches_any(source_branch or "", source_patterns)
    if matched:
        return f"source branch matches skip pattern '{matched}'"

    target_patterns = load_patterns(project.get("skip_target_branches"))
    matched = matches_any(target_branch or "", target_patterns)
    if matched:
        return f"target branch matches skip pattern '{matched}'"

    return None
