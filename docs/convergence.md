# Converging this fork onto upstream

This fork tracks [Sugar-Coffee/stokowski](https://github.com/Sugar-Coffee/stokowski).
In September 2026 it had drifted 26 commits and 131 days behind, and both sides
had rewritten the same modules to answer the same question — run N Linear
projects at once — incompatibly. This records how that was resolved and the
rule that keeps it from recurring.

## The rule

Future merge cost is *(files upstream changes)* ∩ *(files where we differ)*.
Upstream touches `orchestrator.py`, `config.py` and `web.py` in most of its
commits, so **on those files we carry upstream's implementation verbatim** and
add fork features on top as additive layers:

- a new dataclass, or a new field with a default
- a new branch guarded by that field
- a new parameter that is a **defaulted keyword, never a required positional**

Never a restructure of a hot file. `tests/test_fork_api_compat.py` enforces the
keyword rule by parsing every upstream→fork call site and binding it.

Cold files — ones upstream rarely touches — can diverge freely.

## Fork patches on upstream lines

Where the fork must change a line upstream also has, the divergence carries a
tripwire test that fails when a merge reverts it and names the patch in its
failure message. `tests/test_fork_patches.py` is that registry; a document
listing them would go stale the first time someone merged without reading it.

Both are candidates to send upstream. The null-comment-body guard fixes a real
crash: three call sites in upstream's `tracking.py` do `comment.get("body", "")`,
whose default applies only to a *missing* key, so a present-but-null body raises
`TypeError` — and one of them, `parse_latest_tracking`, decides which state an
issue resumes in. Upstream has no coverage for it and their suite is green on
their `main` (269 passed, 1 skipped), so a PR must carry its own failing test.

## Features re-applied

Docker isolation, multi-repo routing, the three-level Docker image hybrid,
repo-aware prompts, triage env injection, agent log retention, and the
`evaluator` state type.

Docker is the shape to copy: before the convergence it had 64 references in
`orchestrator.py` and 36 in `runner.py`; it now has one pass-through helper and
five call-site edits, because containerisation is a launch-point concern and
does not belong threaded through the state machine.

## Features dropped, and why

**Our N-file multi-project model.** Upstream's `MultiOrchestrator` takes one
`workflow_path` and runs every entry in `projects:` — the same capability from
one file. Ours could give each project its own Linear endpoint and upstream's
cannot, but buying that back means re-plumbing the file upstream changes most,
and no shipped config used it.

**Multi-path CLI discovery.** Existed to feed that model. Upstream's own
auto-detection had no coverage at all; that precedence chain is now tested in
`tests/test_workflow_autodetect.py`.

**Attachment-based state tracking.** A genuine loss worth stating plainly: a
single mutable attachment is a better record than an append-only comment log.
It goes anyway because upstream owns state persistence, changes it constantly,
and has since fixed the duplicate-comment problem that motivated attachments —
`_announced_states`. Re-applying it would mean permanent conflict on the
hottest path in the codebase to win a race upstream has already run.

**Derived workflow transitions** (`WorkflowConfig`, `path:` lists). Upstream
uses explicit transitions plus `routing:` rules. Two competing workflow models
in one config is exactly the divergence this exercise removed.

All are recoverable from the `pre-convergence` tag.

## What kept going wrong

Every serious bug found here was silent, and green tests were the reason:

- `ensure_workspace` took `repo_name` third while upstream's orchestrator
  passed `hooks` there — **every dispatch would have raised TypeError**, with
  the suite fully green. Neither suite crossed that seam.
- `docker.enabled: true` parsed and validated, then `_project_view` reset it —
  **agents ran on the host** while every check said they were contained.
- `repos:` was silently discarded while the README told operators to copy that
  config, quietly downgrading a multi-repo team to one repo.
- `pytest-asyncio` was undeclared, so CI was broken on a clean checkout while
  every developer's venv hid it.

The lesson is not "write more tests" — the suite was green throughout. It is
that **a config key which parses for a feature that is absent, and a guard that
cannot fail, both read as coverage.** Hence:

- `UNWIRED_FORK_KEYS` refuses config for a removed feature rather than ignoring
  it. Adding an entry is the cost of removing such a feature.
- `tests/test_project_view.py` compares the parsed config to the runtime one
  field by field, so a field the view forgets is caught without anyone
  remembering to check.
- Guards are mutation-tested: break the code, confirm the guard fails.

## Staying current

`.github/workflows/upstream-drift.yml` runs weekly and fails past 15 commits
behind. The previous automation only tagged a backup; it ran for months and
caught nothing, which is why this one fails instead of reporting.

Routine sync: `git fetch upstream && git merge upstream/main`. **Merge, never
rebase.** The convergence gave this fork a shared merge base rather than a
replayable patch stack, so a rebase replays smithc's absorbed pre-convergence
commits and conflicts against content already in the tree -- reproduced: it dies
on the first commit of the replay, `0ce5da5`, on the four hot files.

### The Release workflow is disabled on this fork

`.github/workflows/release.yml` was disabled on 2026-09-10 via
`gh workflow disable release.yml --repo alxmyth/stokowski`. That is a repository
setting and leaves **no trace in git**, so this note is the only record of it.

Why: it fires on every push to `main` with `contents: write` and can `git tag`
and `gh release create`. It is guarded only by a regex over the head commit's
subject, with no owner check, so it runs here as readily as upstream. Today it
cuts nothing only by accident -- this fork is ~100 commits ahead, so a sync can
never fast-forward, and a merge commit's subject matches neither release
pattern. A fast-forward would flip that to a real tag and release on the fork.

It is disabled from the repo side rather than guarded in the file because
release.yml is byte-identical to upstream's and upstream actively edits it; an
`if: github.repository == ...` guard would plant a patch on a hot upstream file
and would be invisible to `upstream-drift.yml`, which counts `FORK PATCH`
markers under `stokowski/` only.

Do not re-enable it without a replacement guard.
