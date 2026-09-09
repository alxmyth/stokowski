"""Every config this repo ships must be either runnable or explicitly pending.

CI validates `workflow.example.yaml` and nothing else, and its own comment
explains why that matters: "the examples are what operators copy." The other
shipped configs were broken and invisible — the multi-repo examples silently
ignored their `repos:` registry, and the triage example's gates had no
transitions because the fork's `derive_workflow_transitions()` left with the
convergence.

This walks all of them. A config is allowed to fail only if it is named in
PENDING below with the feature it waits on; anything else failing is a real
break. Removing an entry here is part of re-applying its feature.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from stokowski.config import parse_workflow_file, validate_config

REPO = Path(__file__).resolve().parent.parent

# Errors that depend on the environment rather than the file. CI runs without a
# Linear key on purpose, so these are noise here.
ENV_ERRORS = ("missing tracker API key",)

# Configs that cannot validate yet. Value = why. Empty is the goal.
PENDING = {
}


def _shipped() -> list[Path]:
    found = sorted(REPO.glob("workflow*.yaml")) + sorted(REPO.glob("examples/*/*.yaml"))
    # workflow.yaml is the operator's own untracked config, not something we ship.
    return [p for p in found if p.name != "workflow.yaml"]


def _errors(path: Path) -> list[str]:
    errors = validate_config(parse_workflow_file(str(path)).config)
    return [e for e in errors if not any(noise in e for noise in ENV_ERRORS)]


def test_the_walk_finds_the_configs():
    """A discovery test that finds nothing guarantees nothing."""
    names = {p.name for p in _shipped()}
    assert "workflow.example.yaml" in names, "the primary example is not being validated"
    # Three today: the primary example plus the two multi-repo ones. The guard
    # exists so a glob that stops matching cannot make this file vacuous.
    assert len(_shipped()) >= 3, f"only found {len(_shipped())} shipped configs"


@pytest.mark.parametrize("path", _shipped(), ids=lambda p: p.name)
def test_shipped_config_is_runnable_or_declared_pending(path):
    rel = str(path.relative_to(REPO))
    errors = _errors(path)

    if rel in PENDING:
        assert errors, (
            f"{rel} is listed as pending on {PENDING[rel]} but now validates "
            f"clean. If its feature was re-applied, remove it from PENDING."
        )
        return

    assert not errors, (
        f"{rel} ships broken:\n  " + "\n  ".join(errors) +
        f"\n\nOperators copy these. Fix the config, or add it to PENDING with "
        f"the feature it waits on."
    )
