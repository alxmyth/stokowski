"""State machine tracking via structured Linear comments."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("stokowski.tracking")

STATE_PATTERN = re.compile(r"<!-- stokowski:state ({.*?}) -->")
GATE_PATTERN = re.compile(r"<!-- stokowski:gate ({.*?}) -->")
EVAL_PATTERN = re.compile(r"<!-- stokowski:evaluation ({.*?}) -->")

_TIER_FALLBACK = re.compile(
    r"\btier[:\s]+[\"']?(approve|review-required)[\"']?", re.IGNORECASE
)


def build_attachment_metadata(
    state: str,
    type: str,
    run: int = 1,
    workflow: str | None = None,
    status: str | None = None,
    rework_to: str | None = None,
    tier: str | None = None,
    summary: str | None = None,
    findings: list[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Build the metadata dict for a Stokowski attachment."""
    meta: dict[str, Any] = {
        "state": state,
        "type": type,
        "run": run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if workflow is not None:
        meta["workflow"] = workflow
    if session_id is not None:
        meta["session_id"] = session_id
    if status is not None:
        meta["status"] = status
    if rework_to is not None:
        meta["rework_to"] = rework_to
    if tier is not None:
        meta["tier"] = tier
    if summary is not None:
        meta["summary"] = summary
    if findings is not None:
        meta["findings"] = findings
    return meta


def parse_attachment_state(metadata: dict | None) -> dict[str, Any] | None:
    """Convert attachment metadata to the same format as parse_latest_tracking()."""
    if not metadata or "state" not in metadata:
        return None
    result = dict(metadata)
    result.setdefault("type", "state")
    result.setdefault("workflow", None)
    return result


def make_state_comment(
    state: str, run: int = 1, workflow: str | None = None
) -> str | None:
    """State transitions are silent -- conveyed by Linear ticket state change."""
    return None


def make_gate_comment(
    state: str,
    status: str,
    prompt: str = "",
    rework_to: str | None = None,
    run: int = 1,
    workflow: str | None = None,
) -> str | None:
    """Human-readable gate comment for rework/escalation only. None for waiting/approved."""
    if status == "rework":
        text = (
            f"**[Stokowski]** Rework requested at **{state}** "
            f"— returning to **{rework_to}**"
        )
        if run > 1:
            text += f" (run {run})"
        return text
    elif status == "escalated":
        return (
            f"**[Stokowski]** Max rework exceeded at **{state}**. "
            f"Escalating for human intervention."
        )
    return None


def make_evaluation_comment(
    state: str,
    tier: str,
    summary: str = "",
    findings: list[str] | None = None,
    run: int = 1,
    workflow: str | None = None,
) -> str | None:
    """Human-readable evaluation comment for review-required only. None for approve."""
    if tier != "review-required":
        return None
    text = f"**[Stokowski]** Evaluation flagged {len(findings or [])} concern(s)"
    if summary:
        text += f"\n\n{summary}"
    if findings:
        text += "\n\n**Findings:**\n"
        for finding in findings:
            text += f"- {finding}\n"
    return text


def parse_evaluation_tier(
    result_text: str,
) -> tuple[str, str, list[str]]:
    """Parse evaluation tier from agent result text.

    Uses last-match semantics (like TRANSITION_PATTERN) to prevent
    prompt injection from workspace content. Fallback keyword search
    always returns review-required (never approve from fallback).
    Defaults to review-required on any parse failure (fail-safe).
    """
    if not result_text:
        return "review-required", "", []

    # Primary: structured comment (last match wins)
    matches = EVAL_PATTERN.findall(result_text)
    if matches:
        try:
            data = json.loads(matches[-1])
            tier = data.get("tier", "review-required")
            if tier not in ("approve", "review-required"):
                tier = "review-required"
            summary = str(data.get("summary", ""))
            findings = data.get("findings", [])
            findings = [f for f in findings if isinstance(f, str)]
            return tier, summary, findings
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Malformed evaluation JSON, falling back")

    # Fallback: keyword search — always returns review-required
    fallback = _TIER_FALLBACK.search(result_text)
    if fallback:
        logger.info(
            "Evaluation tier detected via keyword fallback, "
            "forcing review-required"
        )
        return "review-required", "", []

    # Default: review-required (fail-safe)
    logger.warning(
        "Could not parse evaluation tier, defaulting to review-required"
    )
    return "review-required", "", []


def parse_latest_tracking(comments: list[dict]) -> dict[str, Any] | None:
    """Parse comments (oldest-first) to find the latest state or gate tracking entry.

    Returns a dict with keys:
        - "type": "state" or "gate"
        - Plus all fields from the JSON payload

    Returns None if no tracking comments found.
    """
    latest: dict[str, Any] | None = None

    for comment in comments:
        body = comment.get("body", "")

        state_match = STATE_PATTERN.search(body)
        if state_match:
            try:
                data = json.loads(state_match.group(1))
                data["type"] = "state"
                data.setdefault("workflow", None)
                latest = data
            except json.JSONDecodeError:
                pass

        gate_match = GATE_PATTERN.search(body)
        if gate_match:
            try:
                data = json.loads(gate_match.group(1))
                data["type"] = "gate"
                data.setdefault("workflow", None)
                latest = data
            except json.JSONDecodeError:
                pass

    return latest


def get_last_tracking_timestamp(comments: list[dict]) -> str | None:
    """Find the timestamp of the latest tracking comment."""
    latest_ts: str | None = None

    for comment in comments:
        body = comment.get("body", "")
        for pattern in (STATE_PATTERN, GATE_PATTERN):
            match = pattern.search(body)
            if match:
                try:
                    data = json.loads(match.group(1))
                    ts = data.get("timestamp")
                    if ts:
                        latest_ts = ts
                except json.JSONDecodeError:
                    pass

    return latest_ts


def get_comments_since(
    comments: list[dict], since_timestamp: str | None
) -> list[dict]:
    """Filter comments to only those after a given timestamp.

    Returns comments that are NOT stokowski tracking comments and
    were created after the given timestamp.
    """
    result = []
    since_dt = None
    if since_timestamp:
        try:
            since_dt = datetime.fromisoformat(
                since_timestamp.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            pass

    for comment in comments:
        body = comment.get("body", "")
        if "<!-- stokowski:" in body:
            continue

        if since_dt:
            created = comment.get("createdAt", "")
            if created:
                try:
                    created_dt = datetime.fromisoformat(
                        created.replace("Z", "+00:00")
                    )
                    if created_dt <= since_dt:
                        continue
                except (ValueError, AttributeError):
                    pass

        result.append(comment)

    return result
