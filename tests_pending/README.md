# Pending re-application

These tests are **not dead**. Each covers a fork feature that was temporarily
removed by the convergence merge with `upstream/main` (Sugar-Coffee), and each
comes back when its feature is re-applied on top of upstream's architecture.

They live outside `tests/` so the suite stays green and honest: a green run
means "everything wired is working", not "everything we own is working". The
count here is the remaining convergence debt.

## Why the features were removed

Our fork and upstream both rewrote the same modules to answer the same question
(run N Linear projects at once) in incompatible ways. Upstream ships fast and we
want cheap recurring syncs, so on the **hot** files — the ones upstream keeps
changing (`orchestrator.py`, `config.py`, `web.py`, `runner.py`, `main.py`,
`prompt.py`, `tracking.py`) — we now carry upstream's implementation verbatim.
Our features return as *additive layers* on top, not as competing rewrites.
That is what makes `git merge upstream/main` cheap from here on.

## What is pending, and what each needs

| Test file | Feature | Needs |
|---|---|---|
| `test_docker_image_hybrid.py` | 3-level image resolution | `config.py` state/repo `docker_image` |
| `test_log_retention.py` | Agent log rotation | `LoggingConfig` + runner wiring |
| `test_attachment_tracking.py` | Linear attachment state | `tracking.py` attachment fns + orchestrator wiring |
| `test_evaluator.py` | `evaluator` state type | `config.py` state type + orchestrator transition |
| `test_state_machine.py` | Derived workflow transitions | `derive_workflow_transitions()` |
| `test_repos_config.py` | `repos:` registry | `RepoConfig` parsing |
| `test_prompt_multirepo.py` | Repo-aware prompts | `prompt.py` repo namespace |
| `test_tracking_multirepo.py` | Repo in tracking payload | attachment metadata `repo` field |
| `test_rejection_coldstart.py` | Rejection recovery | rejection comment parsing |
| `test_triage_env.py` | Triage repo routing | `STOKOWSKI_REPOS_JSON` injection |
| `test_cli_discovery.py` | Multi-path workflow discovery | `main.py` `resolve_workflow_paths()` |
| `test_orchestrator_multi_project.py` | Our N-file multi-project | **likely superseded** by upstream `MultiOrchestrator` |
| `test_multi_project_integration.py` | Our N-file multi-project | **likely superseded** — see above |

## What is already refused, and what merely does nothing

`repos:` is refused by `validate_config` via `UNWIRED_FORK_KEYS` in `config.py`
— accepting it would ignore every `repo:` label and run a multi-repo team
against a single repo. Both `workflow.multi-repo*.example.yaml` files therefore
fail to start, which is deliberate; the top-level README marks them pending.
The check scans project blocks as well as the top level, because `projects:` is
upstream's documented shape and a nested `repos:` bypassed an earlier version.

Docker isolation is **done** — see below.

Delete the corresponding guard when you re-apply the layer — its test says so.
That is not hypothetical: the Docker refusal and its guard test were both
removed in the commit that wired Docker into dispatch, which is the order to
follow. Wire it, prove it, then lift the guard.

### Docker isolation — re-applied

Wired through `_prepare_docker_args` in `runner.py`, a pass-through when docker
is absent or disabled. `run_turn` and both runners take `docker_cfg`,
`docker_image` and `workspace_key` as defaulted keywords; the orchestrator
passes them at both dispatch sites and at all three `remove_workspace` sites.
Docker mode uses `docker_env()`, not `agent_env()` — inheriting the parent
environment into a container defeats the isolation.

Pre-convergence this feature had 64 references in `orchestrator.py`; it now has
five call-site edits. That difference is the whole point of re-applying a
feature onto upstream's structure rather than restoring the old integration.

`tests/test_docker_dispatch.py` asserts the launch path; `tests/test_docker_runner.py`
came back from here with only its `_minimal_service_config` helper rewritten.

`linear.py` also still carries the attachment API (`upsert_`/`fetch_`/
`delete_stokowski_attachment`) with passing tests in `tests/test_attachment_api.py`,
even though nothing calls it — it encodes Linear API details that are expensive
to re-derive. Both ends are labelled. If attachment tracking is abandoned rather
than re-applied, delete the code and its tests together.

`workspace.py` and `docker_runner.py` are present and import cleanly, and
`workspace.py` is call-compatible with upstream's orchestrator (enforced by
`tests/test_fork_api_compat.py`). Nothing dispatches into Docker yet.

An earlier version of this file called them "wired", which is what let a real
arity break ship: our `ensure_workspace` took `repo_name` third while upstream's
orchestrator passed `hooks` there, so every dispatch would have raised
TypeError with the suite fully green. Describe this seam precisely or not at all.

## Re-applying one

1. Add the feature to upstream's structure **additively** — a new dataclass, a
   new field with a default, a new branch guarded by that field. Do not
   restructure a hot file; that is what re-creates the merge debt. A new
   parameter must be a defaulted keyword, never a required positional, or it
   breaks upstream's call sites (`tests/test_fork_api_compat.py` enforces this).
2. Expect to **rewrite the test, not just move it.** These were written against
   the fork's old config API, and several import symbols the convergence
   removed. `test_docker_runner.py` needed only its `_minimal_service_config`
   helper rebuilt on `ProjectConfig`; others may need more. An earlier version
   of this file said "git mv" and that was too optimistic.
3. Note that these files **cross-depend**: `test_docker_image_hybrid.py` is
   blocked on `RepoConfig` and `test_log_retention.py` on `cleanup_old_logs`,
   neither of which is a Docker concern. The table's rows are not independent.
4. `git mv tests_pending/test_x.py tests/`
5. `pytest tests -q` must be green before the next one starts.

## The two marked "likely superseded"

Upstream's `MultiOrchestrator` + `ProjectConfig` already run N projects from one
workflow file. Our model ran N *files* with independent Linear endpoints per
project — genuinely more capable, but re-adding it means re-plumbing the file
upstream changes most. Decide deliberately: if upstream's model is sufficient,
delete these two files rather than carrying them. Do not leave the question open.
