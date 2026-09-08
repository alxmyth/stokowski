"""Patches this fork carries on top of upstream, each with a tripwire.

Convergence keeps our copies of upstream's hot files byte-identical wherever
possible, so `git merge upstream/main` stays cheap. Where we genuinely must
differ, the divergence is recorded here rather than in a document nobody reads:
a future merge that reverts one of these fails a test that names the patch and
says what to do.

Every patch here is also a candidate to send upstream. Once upstream takes one,
its test stops being a tripwire and becomes an ordinary regression test — that
is the goal, because it means the divergence is gone.
"""
from __future__ import annotations

from stokowski.tracking import get_last_tracking_timestamp, parse_latest_tracking


def test_a_null_comment_body_does_not_crash_state_resolution():
    """Linear can return a comment whose body is null; upstream assumes a string.

    `comment.get("body", "")` yields None when the key is present and null — the
    default only applies to a missing key. The tracking parsers then hand None
    to `re.search` and raise TypeError.

    This decides which state an issue resumes in, and it runs on every tick, so
    one bodyless comment on one issue stops that issue advancing. Upstream's own
    suite hits this: three tests in their `test_tracking_order.py` fail on their
    `main` for exactly this reason.

    Patch: `comment.get("body") or ""` at all three sites in tracking.py.
    """
    real = (
        '<!-- stokowski:state {"state": "implement", "run": 1, '
        '"timestamp": "2026-09-01T10:05:00Z"} -->'
    )
    comments = [
        {"id": "1", "body": None, "createdAt": "2026-09-01T10:00:00Z"},
        {"id": "2", "body": real, "createdAt": "2026-09-01T10:05:00Z"},
        {"id": "3", "body": None, "createdAt": "2026-09-01T10:10:00Z"},
    ]

    latest = parse_latest_tracking(comments)
    assert latest is not None, "a null body swallowed the real tracking entry"
    assert latest["state"] == "implement"
    assert latest["type"] == "state"

    assert get_last_tracking_timestamp(comments) == "2026-09-01T10:05:00Z"

    # A list of nothing but bodyless comments must resolve to None, not raise.
    assert parse_latest_tracking([{"id": "1", "body": None, "createdAt": "x"}]) is None
