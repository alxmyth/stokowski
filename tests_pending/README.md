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
| `test_docker_runner.py` | Docker agent isolation | `runner.py` dispatch hooks |
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

Two of these features have config that still parses, so each is rejected at
validation rather than accepted and ignored:

- `docker.enabled: true` — refused by `validate_config`. Accepting it would run
  agents on the host while the operator believed they were contained.
- `repos:` — refused by `validate_config` via `UNWIRED_FORK_KEYS` in
  `config.py`. Both `workflow.multi-repo*.example.yaml` files therefore fail to
  start, which is deliberate; the README marks them pending.

Delete the corresponding guard when you re-apply the layer — its test says so.

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
   restructure a hot file; that is what re-creates the merge debt.
2. `git mv tests_pending/test_x.py tests/`
3. `pytest tests -q` must be green before the next one starts.

## The two marked "likely superseded"

Upstream's `MultiOrchestrator` + `ProjectConfig` already run N projects from one
workflow file. Our model ran N *files* with independent Linear endpoints per
project — genuinely more capable, but re-adding it means re-plumbing the file
upstream changes most. Decide deliberately: if upstream's model is sufficient,
delete these two files rather than carrying them. Do not leave the question open.
