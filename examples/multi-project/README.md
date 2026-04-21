# Multi-project example

Two complete project files demonstrating one Stokowski orchestrator polling N Linear projects at once.

## Run

```bash
stokowski examples/multi-project/
```

The directory mode discovers both `workflow.*.yaml` files, sorts case-insensitively, and binds them as alpha → beta. Invoking with explicit args works too:

```bash
stokowski examples/multi-project/workflow.alpha.yaml examples/multi-project/workflow.beta.yaml
```

Or set it once in `.env` at the repo root and run with no args:

```bash
# .env
STOKOWSKI_WORKFLOW_PATH=examples/multi-project/
```

```bash
stokowski
```

## What to notice

- **Primary file:** `workflow.alpha.yaml` (sorts first). Its `agent.max_concurrent_agents` and `server.port` apply globally. Beta's corresponding fields are parsed but unused — see the inline comments in `workflow.beta.yaml`.
- **Min-across-files:** `polling.interval_ms` — beta's 10s wins over alpha's 30s.
- **Per-project state names:** alpha uses "In Progress" / "Human Review" / "Done"; beta uses "Working" / "In Review" / "Shipped". A ticket's eligibility is evaluated against its own project's state names via `_cfg_for_issue(issue.id)`.
- **Per-project API keys:** `$ALPHA_LINEAR_API_KEY` and `$BETA_LINEAR_API_KEY` resolve from env independently.
- **Per-project repos, workflows, hooks:** alpha is single-repo; beta has three repos with a triage workflow.

## Validate without dispatching

```bash
stokowski --dry-run examples/multi-project/
```

Prints one validation block per project file, including which repos and workflows were parsed and what Linear state names map to each lifecycle role.
