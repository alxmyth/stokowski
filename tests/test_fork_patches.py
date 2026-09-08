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


def test_root_level_on_stage_enter_can_actually_fire():
    """The mirror of the after_create bug, and another patch on upstream's line.

    Upstream guards on `state_cfg.hooks.on_stage_enter`, reading the state's
    block directly and bypassing `merge_state_config`. A root-level
    `on_stage_enter` — which `_parse_full_hooks` accepts — therefore never runs,
    because a state with no hooks block makes the guard False.

    Asserted by parsing the guard, since it sits inside the async worker. If a
    merge restores upstream's form, this fails and names the patch.
    """
    import ast

    from tests.test_fork_api_compat import ORCHESTRATOR  # noqa: PLC0415

    src = ORCHESTRATOR.read_text()
    assert "if hooks_cfg.on_stage_enter:" in src, (
        "orchestrator.py no longer guards on_stage_enter with hooks_cfg. Reading "
        "state_cfg.hooks directly means a root-level on_stage_enter never fires. "
        "If a merge reverted this, re-apply the fork patch and send it upstream."
    )
    assert "state_cfg.hooks.on_stage_enter" not in src, (
        "orchestrator.py still reads state_cfg.hooks.on_stage_enter directly"
    )
    ast.parse(src)


def test_run_attempt_declares_result_text_once():
    """An auto-merge left two `result_text` fields on RunAttempt.

    Both defaulted to "", so the dataclass kept the first field's position and
    the second's value and nothing complained. Changing either default or type
    would have made one silently win, with no error and no failing test.
    """
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent
                      / "stokowski" / "models.py").read_text())
    run_attempt = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RunAttempt"
    )
    names = [
        n.target.id for n in run_attempt.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    ]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"RunAttempt declares {sorted(dupes)} more than once"
