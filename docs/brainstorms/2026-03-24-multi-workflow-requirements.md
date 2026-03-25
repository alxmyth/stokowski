---
date: 2026-03-24
topic: multi-workflow-support
---

# Multi-Workflow Support

## Problem Frame

Stokowski currently supports a single workflow definition per config file — one state machine, one entry point, one path through stages. This forces all issues through the same pipeline regardless of what kind of work they represent.

In practice, different types of work need different workflows:
- **Idea refinement** needs just a thinking stage, no code, no MR
- **Research** needs investigation and synthesis, no code, no MR
- **Quick fixes** need a streamlined plan → implement → review → merge path
- **Full compound engineering** needs the complete multi-gate pipeline with plan review, code review, and merge review

Operators currently have no way to express this. They must either run separate Stokowski instances with different configs, or force all work through the heaviest workflow.

## Requirements

### Core Engine

- R1. **Named workflows as paths through shared stages.** All stages (agent, gate, terminal) are defined once in a shared pool. Each named workflow declares an ordered `path` — a sequence of stage names from the pool. Transitions are derived from the path: `complete` goes to the next stage, gate `approve` goes forward, gate `rework_to` goes backward to the specified stage.

- R2. **Label-based workflow selection.** Each workflow declares a Linear label (e.g., `workflow:refinement`). When an issue is fetched, its labels are checked against workflow declarations. First match wins.

- R3. **Default workflow fallback.** One workflow is marked `default: true`. Issues without a matching workflow label are routed to the default workflow.

- R4. **Backward compatibility.** If the config file contains `states:` but no `workflows:` section, all states are treated as a single implicit default workflow with transitions derived from the existing `transitions` declarations. Existing configs continue to work without modification.

### Workflow Definitions

- R5. **Refinement workflow.** Single agent stage that reads the issue, fleshes out the idea, and posts findings as a comment/update on the ticket. Path: `refine → done`. No code, no MR, no branch.

- R6. **Research workflow.** Single agent stage for product, market, or codebase research. Posts findings on the ticket. Path: `research → done`. No code, no MR, no branch.

- R7. **Quick-fix workflow.** Streamlined implementation path without plan-review or merge-review gates. Path: `plan → implement → code-review → merge → done`. Designed to ship faster for low-risk, well-scoped work.

- R8. **Full compound engineering workflow.** The current multi-gate pipeline. Path: `plan → plan-review → implement → code-review → merge-review → merge → done` (or equivalent to current config). Human gates at plan-review and merge-review.

### Triage Agent

- R9. **Triage workflow for automated classification.** A dedicated workflow (`path: [triage, done]`) where a triage agent reads the issue, classifies the type of work, and applies the appropriate `workflow:*` label. The triage workflow is the natural default for unlabeled issues.

- R10. **Triage as label suggestion, not direct routing.** The triage agent only applies labels — it does not directly route issues into workflows. After triage completes, the issue re-enters the dispatch cycle with its new label and is routed normally. This keeps the triage agent's output auditable and overridable. Re-entry uses a configurable terminal Linear state (see R15).

- R15. **Configurable terminal Linear state per workflow.** Each workflow can declare a `terminal_state` that overrides which Linear state the issue moves to on completion. Default is `"terminal"` (Done/Closed/Cancelled). The triage workflow sets `terminal_state: "todo"` so the issue recycles to the pickup state with its new label. All existing terminal behavior (workspace cleanup, tracking state cleared) applies regardless of target Linear state.

- R11. **Triage effectiveness measurement.** Operators should be able to observe triage decisions (via the applied label and any posted rationale comment) and override them by changing the label before or during execution.

### Manual Navigation

- R12. **Workflow switching via label change.** If a human changes an issue's workflow label mid-flight, the next dispatch cycle routes it to the new workflow. If the issue's current stage exists in the new workflow, execution continues from that stage. If not, execution restarts from the new workflow's entry.

- R13. **Stage skipping via gate skip labels.** Existing `skip_labels` mechanism on gates continues to work, allowing operators to auto-approve specific gates for specific issues.

### Workspace Model

- R14. **Workspace optionality (design for, always clone in v1).** The workflow model should support declaring whether a workflow needs a workspace (git clone + hooks). In the initial implementation, all workflows get a workspace. Non-code workflows (refinement, research) can be optimized to skip workspace creation in a future iteration.

## Success Criteria

- Multiple named workflows can be defined in a single config file alongside a shared stage pool
- Labels on Linear issues correctly route to the matching workflow
- The triage agent successfully classifies issues and applies workflow labels
- Existing single-workflow configs (no `workflows:` section) continue to work without changes
- Operators can manually override workflow selection by changing labels
- Quick-fix workflow demonstrably reaches the MR gate faster than full-ce for equivalent work

## Scope Boundaries

- **Not in scope: automated gate classifier.** The "can this skip human review?" decision remains manual (via skip labels). Automated gate classification is a future capability that builds on the triage agent pattern once trust is established.
- **Not in scope: non-linear workflows.** Branching, parallel stages, or conditional paths. Workflows are strictly ordered sequences.
- **Not in scope: per-workflow stage overrides.** A stage behaves the same regardless of which workflow includes it. If different behavior is needed, define a separate stage (e.g., `implement-quick` vs `implement`).
- **Not in scope: workspace-less execution.** All workflows get a workspace in v1, even if they don't produce code.

## Key Decisions

- **Stages are atoms, workflows are molecules**: Stages defined once, reused across workflows via path references. This maximizes composability and minimizes config duplication.
- **Labels as the universal control surface**: Workflow selection, stage skipping, and manual overrides all operate through Linear labels. Visible, auditable, changeable by both humans and agents.
- **Triage agent suggests, doesn't route**: Progressive trust model — start with label suggestions, measure accuracy, graduate to auto-routing later. The triage agent is just another workflow, not a special mechanism.
- **Transitions derived from path order**: Forward transitions come from position in the path. No need to declare transitions per-workflow. Gates derive rework targets from path position (or explicit `rework_to` on the state definition).
- **Configurable terminal Linear state**: A workflow's terminal state can recycle the issue to a different Linear state (e.g., To-Do) instead of Done. This enables triage-then-dispatch without special-case logic. The one-tick latency between triage and the next workflow is a feature — it gives humans a window to override the triage label.

## Dependencies / Assumptions

- Linear supports enough custom labels for workflow routing (no known limit concern)
- The triage agent can reliably classify work types from issue title + description alone
- Operators are willing to adopt a labeling convention for workflow selection

## Outstanding Questions

### Deferred to Planning

- [Affects R1] [Technical] How should transition derivation work for gates with `rework_to`? Should it always point to the previous agent state in the path, or respect the explicit `rework_to` field on the state definition?
- [Affects R4] [Technical] What's the cleanest migration path for existing configs? Detect absence of `workflows:` and synthesize a single workflow, or require explicit migration?
- [Affects R12] [Technical] When a workflow label changes mid-flight and the current stage doesn't exist in the new workflow, should execution restart from the new workflow's entry, or should the issue be returned to To-Do?
- [Affects R14] [Needs research] What's the minimum viable workspace for non-code workflows? Could they run with just a temp directory (no git clone) while still having access to Claude Code tools?
- [Affects R9] [Needs research] What prompt structure gives the triage agent reliable classification? Should it have access to the codebase for context, or just the issue content?

## Next Steps

→ `/ce:plan` for structured implementation planning
