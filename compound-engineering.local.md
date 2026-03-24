---
review_agents:
  - compound-engineering:review:correctness-reviewer
  - compound-engineering:review:maintainability-reviewer
  - compound-engineering:review:reliability-reviewer
  - compound-engineering:review:security-reviewer
---

Python asyncio project. Agent orchestrator that spawns Claude Code as subprocesses. No automated test suite — verification is manual via `--dry-run`.
