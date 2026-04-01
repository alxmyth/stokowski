# Attachment-Based State Tracking

**Problem:** Stokowski posts `<!-- stokowski:state/gate/evaluation {...} -->` HTML comments on Linear issues for crash recovery and state tracking. These machine-readable JSON payloads pollute the human-readable ticket timeline.

**Solution:** Move machine state to Linear's Attachment `metadata` field (API-only, invisible in UI). Post human-readable comments only when they carry information the ticket state doesn't already convey.

---

## Attachment as state store

Each issue gets a single Stokowski-owned attachment:

- **URL:** `stokowski://state/{issue_identifier}` (stable key for idempotent upsert)
- **Title:** `Stokowski`
- **Subtitle:** Human-readable glance, e.g. `implement · run 3` (updated on each transition)
- **Metadata:** Full machine state as JSON

### Metadata schema

```json
{
  "state": "implement",
  "type": "state",
  "run": 3,
  "workflow": "default",
  "session_id": "abc-123",
  "timestamp": "2026-04-01T12:00:00Z",

  "status": "waiting",
  "rework_to": "implement",

  "tier": "approve",
  "summary": "LGTM",
  "findings": []
}
```

Fields are flat (not nested by type). Gate-specific fields (`status`, `rework_to`) are present only when `type == "gate"`. Evaluation-specific fields (`tier`, `summary`, `findings`) are present only when `type == "evaluation"`.

### Lifecycle

- **Create/update:** `attachmentCreate` with the same URL on the same issue = upsert (no duplicates). Called on every state transition, gate entry, and evaluation completion.
- **Read:** `attachmentsForURL("stokowski://state/{id}")` returns current state in a single API call.
- **Delete:** On terminal state, remove the attachment (clean up).

---

## Comment policy

Comments fire only when they carry information the Linear ticket state doesn't convey:

| Event | Comment? | Content |
|-------|----------|---------|
| State entry | No | Ticket moves to "In Progress" — visible |
| Gate waiting | No | Ticket moves to "Human Review" — visible |
| Gate approved | No | Ticket moves to "In Progress" — visible |
| Gate rework | **Yes** | "Rework requested at **merge-review** — returning to **implement** (run 2)" |
| Gate escalated | **Yes** | "Max rework exceeded at **merge-review** — escalating for human intervention" |
| Evaluation approve | No | Silent optimization (gate skipped) |
| Evaluation findings | **Yes** | "Evaluation flagged N concerns" + findings list |

All comments are plain human-readable text. No `<!-- stokowski:... -->` JSON payloads.

---

## Crash recovery

**Primary:** Fetch the attachment metadata — single API call, returns full state.

**Degraded fallback:** If no attachment exists (deleted manually, or pre-migration issue), infer state from the Linear ticket state name. This gives the correct stage but not the run number (resets to 1). Acceptable for a rare edge case.

**Migration path:** During transition, check for attachment first. If not found, fall back to the existing comment-scanning path (`parse_latest_tracking`). Once all in-flight issues have cycled through, comment-scanning code can be removed.

---

## Linear API additions

Three new methods in `linear.py`:

```python
async def upsert_stokowski_attachment(
    self, issue_id: str, identifier: str, metadata: dict, subtitle: str
) -> bool

async def fetch_stokowski_attachment(
    self, identifier: str
) -> dict | None

async def delete_stokowski_attachment(
    self, identifier: str
) -> bool
```

- `upsert` uses `attachmentCreate` (idempotent by URL + issue).
- `fetch` uses `attachmentsForURL`.
- `delete` finds and removes the attachment.

---

## Change surface

| File | Changes |
|------|---------|
| `linear.py` | Add 3 attachment methods (upsert, fetch, delete) |
| `tracking.py` | `make_*_comment()` drop JSON, return human-only text (rework/escalation/evaluation only). New `build_attachment_metadata()` helper. `parse_latest_tracking()` reads attachment first, falls back to comment scan |
| `orchestrator.py` | Transition sites: upsert attachment, then conditionally post comment. `_resolve_current_state()` reads attachment first. Terminal cleanup deletes attachment. |
| `prompt.py` | `get_comments_since()` keeps `<!-- stokowski:` filter for migration safety |
| `models.py` | No changes |
| `runner.py` | No changes |

No new files. No new dependencies.
